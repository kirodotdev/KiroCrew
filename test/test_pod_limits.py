"""Per-pod ``--memory`` / ``--cpu`` ceilings and ``pod restart``, which keeps the home.

The fixtures come from ``pod_helpers.py``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

import pytest
from pod_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _cp,
    _fake_node_toolchain,
    _isolate_pod_host_state,
    _ready_worktree,
    _systemd_backend,
    requires_posix_pod_lifecycle,
)

from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import limits as pod_limits
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod.config import PodConfig

_GIB = 1024**3


@pytest.fixture
def host_16g_8cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fixed host so the caps do not depend on the runner's size."""
    monkeypatch.setattr(pod_limits, "host_memory_cap_bytes", lambda: int(16 * 0.8 * 1024) * 1024**2)
    monkeypatch.setattr(pod_limits, "host_cpu_cap_percent", lambda: 800)


class TestParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("12G", "12G"), ("12g", "12G"), ("8192M", "8G"), ("1536M", "1536M"), ("512M", "512M")],
    )
    def test_memory_is_normalised_to_systemd_spelling(
        self, raw: str, expected: str, host_16g_8cpu: None
    ) -> None:
        assert pod_limits.normalize_memory(raw) == expected

    @pytest.mark.parametrize("raw", ["", "16", "16GB", "-1G", "0G", "1.5G", "16 G", "infinity"])
    def test_malformed_memory_is_refused(self, raw: str, host_16g_8cpu: None) -> None:
        with pytest.raises(pod_limits.LimitError):
            pod_limits.normalize_memory(raw)

    def test_memory_below_the_floor_is_refused(self, host_16g_8cpu: None) -> None:
        with pytest.raises(pod_limits.LimitError, match="floor"):
            pod_limits.normalize_memory("256M")

    def test_memory_above_the_host_cap_is_refused_naming_the_cap(self, host_16g_8cpu: None) -> None:
        # 80% of 16 GiB is 13107M, so 14G is over and the message says what fits.
        with pytest.raises(pod_limits.LimitError, match="13107M"):
            pod_limits.normalize_memory("14G")
        assert pod_limits.normalize_memory("12G") == "12G"

    @pytest.mark.parametrize(("raw", "expected"), [("800%", "800%"), ("8", "800%"), ("50%", "50%")])
    def test_cpu_accepts_a_percentage_or_a_core_count(
        self, raw: str, expected: str, host_16g_8cpu: None
    ) -> None:
        assert pod_limits.normalize_cpu(raw) == expected

    @pytest.mark.parametrize("raw", ["", "8.5", "800", "%", "-2", "5%", "900%"])
    def test_bad_or_out_of_range_cpu_is_refused(self, raw: str, host_16g_8cpu: None) -> None:
        with pytest.raises(pod_limits.LimitError):
            pod_limits.normalize_cpu(raw)

    def test_an_unreadable_host_size_skips_only_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pod_limits, "host_memory_cap_bytes", lambda: None)
        monkeypatch.setattr(pod_limits, "host_cpu_cap_percent", lambda: None)
        assert pod_limits.normalize_memory("64G") == "64G"
        assert pod_limits.normalize_cpu("6400%") == "6400%"
        with pytest.raises(pod_limits.LimitError):
            pod_limits.normalize_memory("1M")

    def test_the_real_host_cap_is_positive(self) -> None:
        cap = pod_limits.host_memory_cap_bytes()
        assert cap is None or cap > 0
        cpu = pod_limits.host_cpu_cap_percent()
        assert cpu is None or cpu >= 100


class TestFromEnv:
    def test_no_keys_means_the_template_defaults(self) -> None:
        lim = pod_limits.from_env({"CHECKOUT": "/x"})
        assert (lim.memory, lim.cpu) == (pod_limits.DEFAULT_MEMORY, pod_limits.DEFAULT_CPU)
        assert lim.memory_default and lim.cpu_default

    def test_recorded_keys_are_revalidated(self, host_16g_8cpu: None) -> None:
        lim = pod_limits.from_env({"MEMORY": "8G", "CPU": "400%"})
        assert lim.as_json() == {"memory": "8G", "cpu": "400%"}
        assert not lim.memory_default and not lim.cpu_default
        with pytest.raises(pod_limits.LimitError):
            pod_limits.from_env({"MEMORY": "64G"})

    def test_describe_shows_a_bad_hand_edit_instead_of_raising(self, host_16g_8cpu: None) -> None:
        assert pod_limits.describe({"MEMORY": "lots"}).memory == "lots"


class TestUnitRendering:
    def test_template_carries_the_defaults(self) -> None:
        text = unit_mod.render_unit(PodConfig.load())
        assert f"MemoryMax={pod_limits.DEFAULT_MEMORY}\n" in text
        assert f"CPUQuota={pod_limits.DEFAULT_CPU}\n" in text

    def test_default_ceilings_leave_the_dropin_unchanged(self, tmp_path: Path) -> None:
        plain = unit_mod.render_dropin(tmp_path)
        assert unit_mod.render_dropin(tmp_path, pod_limits.from_env({})) == plain
        assert "MemoryMax" not in plain and "CPUQuota" not in plain

    def test_set_ceilings_are_appended_to_the_dropin(
        self, tmp_path: Path, host_16g_8cpu: None
    ) -> None:
        text = unit_mod.render_dropin(tmp_path, pod_limits.from_env({"MEMORY": "12G"}))
        assert text.endswith("MemoryMax=12G\n")
        assert "CPUQuota" not in text
        both = unit_mod.render_dropin(
            tmp_path, pod_limits.from_env({"MEMORY": "12G", "CPU": "800%"})
        )
        assert "MemoryMax=12G\n" in both and "CPUQuota=800%\n" in both


@requires_posix_pod_lifecycle
class TestLifecycle:
    def _cfg(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        c = PodConfig.load()
        # `pod install` creates the user unit directory; a drop-in lives beside it.
        unit_mod.unit_path(c).parent.mkdir(parents=True, exist_ok=True)
        return c

    def test_start_writes_the_ceiling_into_the_dropin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_16g_8cpu: None
    ) -> None:
        c = self._cfg(tmp_path, monkeypatch)
        rt.write_env_file(c, "demo", {"CHECKOUT": str(tmp_path / "wt"), "MEMORY": "12G"})
        monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda cfg: True)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        assert rt.start_pod(c, "demo").returncode == 0
        assert "MemoryMax=12G" in unit_mod.dropin_path(c, "demo").read_text()

    def test_start_refuses_a_hand_edited_bad_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_16g_8cpu: None
    ) -> None:
        c = self._cfg(tmp_path, monkeypatch)
        rt.write_env_file(c, "demo", {"CHECKOUT": str(tmp_path / "wt"), "CPU": "lots"})
        monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda cfg: True)
        calls: list[tuple] = []
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: calls.append(a) or _cp(returncode=0))
        cp = rt.start_pod(c, "demo")
        assert cp.returncode == 1 and "invalid ceiling" in cp.stderr
        assert not any(a[0] == "start" for a in calls)

    def test_restart_halts_then_starts_and_never_reclaims(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._cfg(tmp_path, monkeypatch)
        from kiro_crew.pod import runtime_lifecycle as lc

        steps: list[str] = []

        monkeypatch.setattr(lc, "halt_pod", lambda cfg, n: steps.append("halt") or _cp())
        monkeypatch.setattr(lc, "start_pod", lambda cfg, n: steps.append("start") or _cp())
        monkeypatch.setattr(lc, "stop_pod", lambda cfg, n: steps.append("stop") or _cp())
        assert lc.restart_pod(c, "demo").returncode == 0
        assert steps == ["halt", "start"]

    def test_restart_starts_nothing_when_the_halt_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.pod import runtime_lifecycle as lc

        c = self._cfg(tmp_path, monkeypatch)
        started: list[str] = []
        monkeypatch.setattr(lc, "halt_pod", lambda cfg, n: _cp(returncode=1, stderr="stuck"))
        monkeypatch.setattr(lc, "start_pod", lambda cfg, n: started.append(n) or _cp())
        assert lc.restart_pod(c, "demo").returncode == 1
        assert started == []

    def test_restart_keeps_a_real_home_on_disk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Through the systemd adapter: stop and start, and the home is untouched."""
        from kiro_crew.pod import runtime_lifecycle as lc

        c = self._cfg(tmp_path, monkeypatch)
        rt.write_env_file(c, "demo", {"CHECKOUT": str(tmp_path / "wt")})
        home = c.home_dir("demo")
        home.mkdir(parents=True)
        (home / "kept.txt").write_text("state")
        verbs: list[str] = []

        def _systemctl(*args: str, **_k: object) -> subprocess.CompletedProcess:
            verbs.append(args[0])
            return _cp(returncode=0, stdout="")

        monkeypatch.setattr(rt, "systemctl", _systemctl)
        monkeypatch.setattr(rt.unit_mod, "unit_is_current", lambda cfg: True)
        assert lc.restart_pod(c, "demo").returncode == 0
        assert (home / "kept.txt").read_text() == "state"
        assert verbs.index("stop") < verbs.index("start")


@requires_posix_pod_lifecycle
class TestCli:
    def _prep(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, active: bool) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_WORKTREES_ROOT", str(tmp_path / "wts"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        _ready_worktree(tmp_path / "wts", "demo")
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: active)
        monkeypatch.setattr(rt, "health", lambda cfg, n, p: 403)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        return PodConfig.load()

    def _up_args(self, **kw: object) -> argparse.Namespace:
        base = dict(name="demo", json=True, seed="", ttl="2h", provision=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_up_records_normalised_ceilings_and_reports_them(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, host_16g_8cpu: None
    ) -> None:
        c = self._prep(tmp_path, monkeypatch, active=False)
        pod_cli._up(c, self._up_args(memory="8192M", cpu="4"))
        env = rt.read_env_file(c, "demo")
        assert (env["MEMORY"], env["CPU"]) == ("8G", "400%")
        handle = json.loads(capsys.readouterr().out)
        assert (handle["memory"], handle["cpu"]) == ("8G", "400%")

    def test_up_refuses_an_over_cap_value_before_writing_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, host_16g_8cpu: None
    ) -> None:
        c = self._prep(tmp_path, monkeypatch, active=False)
        with pytest.raises(SystemExit):
            pod_cli._up(c, self._up_args(memory="64G"))
        assert "cap" in capsys.readouterr().err
        assert not c.env_file("demo").exists()

    def test_status_and_ls_json_carry_the_ceilings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, host_16g_8cpu: None
    ) -> None:
        c = self._prep(tmp_path, monkeypatch, active=True)
        rt.write_env_file(c, "demo", {"CHECKOUT": "/x", "MEMORY": "12G"})
        pod_cli._status(c, argparse.Namespace(name="demo", json=True))
        row = json.loads(capsys.readouterr().out)
        assert (row["memory"], row["cpu"]) == ("12G", pod_limits.DEFAULT_CPU)
        monkeypatch.setattr(rt, "active_names", lambda cfg: {"demo"})
        pod_cli._ls(c, argparse.Namespace(json=True))
        rows = json.loads(capsys.readouterr().out)
        assert rows == [
            {"name": "demo", "port": 7811, "health": 403, "memory": "12G", "cpu": "200%"}
        ]

    def test_restart_records_new_ceilings_and_keeps_the_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, host_16g_8cpu: None
    ) -> None:
        c = self._prep(tmp_path, monkeypatch, active=True)
        rt.write_env_file(c, "demo", {"CHECKOUT": str(tmp_path / "wts" / "demo")})
        restarted: list[str] = []
        monkeypatch.setattr(rt, "restart_pod", lambda cfg, n: restarted.append(n) or _cp())
        monkeypatch.setattr(
            rt, "stop_pod", lambda cfg, n: pytest.fail("restart must not reclaim the home")
        )
        pod_cli._restart(
            c,
            argparse.Namespace(name="demo", json=True, wait_secs=None, memory="12G", cpu="800%"),
        )
        assert restarted == ["demo"]
        env = rt.read_env_file(c, "demo")
        assert (env["MEMORY"], env["CPU"]) == ("12G", "800%")
        handle = json.loads(capsys.readouterr().out)
        assert handle["home_kept"] is True and handle["cpu"] == "800%"

    def test_restart_without_a_pinned_checkout_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch, active=False)
        monkeypatch.setattr(rt, "restart_pod", lambda cfg, n: pytest.fail("nothing to restart"))
        with pytest.raises(SystemExit):
            pod_cli._restart(
                c,
                argparse.Namespace(name="demo", json=False, wait_secs=None, memory=None, cpu=None),
            )
        assert "pod up demo" in capsys.readouterr().err

    def test_parser_accepts_the_new_flags(self) -> None:
        from kiro_crew import cli_parser_admin

        parser = argparse.ArgumentParser()
        cli_parser_admin.register_pod_parser(parser.add_subparsers(dest="command"))
        up = parser.parse_args(["pod", "up", "wt", "--memory", "16G", "--cpu", "800%"])
        assert (up.memory, up.cpu) == ("16G", "800%")
        rs = parser.parse_args(["pod", "restart", "wt", "--memory", "8G", "--json"])
        assert (rs.pod_action, rs.memory, rs.cpu, rs.json) == ("restart", "8G", None, True)


class TestRestartEdges:
    def test_windows_restart_refuses_before_stopping_anything(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Windows start refuses the state a Windows stop keeps, so nothing is halted."""
        from kiro_crew.pod import runtime_lifecycle as lc

        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setattr(rt, "IS_WINDOWS", True)
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setattr(lc, "halt_pod", lambda cfg, n: pytest.fail("must not stop"))
        cp = lc.restart_pod(PodConfig.load(), "demo")
        assert cp.returncode == 1
        assert "not supported on Windows" in cp.stderr and "Nothing was stopped" in cp.stderr


@requires_posix_pod_lifecycle
class TestRestartHealthFailure:
    def test_a_failed_health_wait_halts_the_unit_and_keeps_the_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        """The started unit restarts every 5s on failure, so the CLI stops it, never reclaims."""
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        monkeypatch.setattr(rt, "IS_MACOS", False)
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: None)
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda *a, **k: -1)
        c = PodConfig.load()
        rt.write_env_file(c, "demo", {"CHECKOUT": str(tmp_path / "wt")})
        steps: list[str] = []
        monkeypatch.setattr(rt, "restart_pod", lambda cfg, n: steps.append("restart") or _cp())
        monkeypatch.setattr(rt, "halt_pod", lambda cfg, n: steps.append("halt") or _cp())
        monkeypatch.setattr(rt, "stop_pod", lambda cfg, n: pytest.fail("must not reclaim"))
        with pytest.raises(SystemExit):
            pod_cli._restart(
                c, argparse.Namespace(name="demo", json=False, wait_secs=5, memory=None, cpu=None)
            )
        assert steps == ["restart", "halt"]
        assert "stopped and its home was kept" in capsys.readouterr().err


def test_a_restart_refused_for_the_user_bus_is_audited_as_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The audit trail names the verb the operator ran, not the one the helper serves first."""
    seen: list[tuple] = []
    monkeypatch.setattr(rt, "IS_LINUX", True)

    def _no_bus() -> None:
        raise rt.PodError("no user bus")

    monkeypatch.setattr(rt, "require_backend", _no_bus)
    monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: seen.append(a))
    with pytest.raises(rt.PodError):
        pod_cli._restart(
            PodConfig.load(),
            argparse.Namespace(name="demo", json=False, wait_secs=None, memory=None, cpu=None),
        )
    assert [a[:2] for a in seen] == [("pod.restart", "failure")]


@requires_posix_pod_lifecycle
def test_restart_refuses_a_bad_stored_ceiling_before_stopping_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_16g_8cpu: None
) -> None:
    """The start would refuse it, so checking only then leaves a healthy pod stopped."""
    from kiro_crew.pod import runtime_lifecycle as lc

    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setattr(rt, "IS_WINDOWS", False)
    c = PodConfig.load()
    rt.write_env_file(c, "demo", {"CHECKOUT": str(tmp_path / "wt"), "CPU": "lots"})
    monkeypatch.setattr(lc, "halt_pod", lambda cfg, n: pytest.fail("must not stop"))
    cp = lc.restart_pod(c, "demo")
    assert cp.returncode == 1
    assert "invalid ceiling" in cp.stderr and "Nothing was stopped" in cp.stderr
