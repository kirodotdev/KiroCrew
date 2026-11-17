"""``pod up --env KEY=VALUE``: allowlisted test-double overrides for one pod.

Checked at ``pod up``, recorded in the per-pod env file, re-checked and applied at
boot after the env scrub, and honoured by the child-viability probe. The fixtures
come from ``pod_helpers.py``.
"""

from __future__ import annotations

import argparse
import os
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
from kiro_crew.pod import env_overrides
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import runtime_boot
from kiro_crew.pod.config import PodConfig

KEY = env_overrides.KIRO_BIN_KEY
LINE_BREAKS = ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\n", "\r")


def _fake_bin(tmp_path: Path, *, executable: bool = True) -> Path:
    """A fake kiro-cli. The ``.py`` suffix keeps it runnable on Windows too."""
    path = tmp_path / "fake-kiro-cli.py"
    path.write_text(
        "import sys\nsys.stderr.write(\"error: unexpected argument '--auth-method'\")\n"
    )
    path.chmod(0o755 if executable else 0o644)
    return path


# Parsing and the allowlist


def test_an_allowed_key_with_an_executable_path_is_accepted(tmp_path: Path) -> None:
    fake = _fake_bin(tmp_path)
    assert env_overrides.parse_assignments([f"{KEY}={fake}"]) == {KEY: str(fake)}


@pytest.mark.parametrize(
    "entry",
    [
        "AWS_SECRET_ACCESS_KEY=abc",
        "SLACK_BOT_TOKEN=xoxb-1",
        "PATH=/tmp",
        "KIROCREW_HOME=/tmp/x",
    ],
)
def test_a_key_outside_the_allowlist_is_refused(entry: str) -> None:
    # The allowlist is what keeps secrets and isolation knobs out of a pod: the
    # overrides are applied after the scrub, so any key admitted here would land.
    with pytest.raises(env_overrides.EnvOverrideError, match="not an allowed key"):
        env_overrides.parse_assignments([entry])


def test_an_unusable_kiro_bin_is_refused(tmp_path: Path) -> None:
    # The resolver silently skips a path that is not an executable file and falls
    # back to the real kiro-cli, so each of these would boot a pod that only looks
    # overridden.
    for entry, why in (
        (f"{KEY}=relative/kiro-cli", "not an absolute path"),
        (f"{KEY}={tmp_path / 'missing'}", "not an executable file"),
        (f"{KEY}={tmp_path}", "not an executable file"),
    ):
        with pytest.raises(env_overrides.EnvOverrideError, match=why):
            env_overrides.parse_assignments([entry])


def test_an_empty_kiro_bin_is_refused(tmp_path: Path) -> None:
    # Executable discovery never runs an empty file, so it could not be the child.
    empty = tmp_path / "empty-kiro-cli.py"
    empty.write_text("")
    empty.chmod(0o755)
    with pytest.raises(env_overrides.EnvOverrideError, match="empty file"):
        env_overrides.parse_assignments([f"{KEY}={empty}"])


def test_a_kiro_bin_that_vanishes_mid_check_is_refused_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Boot re-checks the value; an OSError there must be a refusal it can log.
    fake = _fake_bin(tmp_path)

    def _gone(_p: object) -> int:
        raise FileNotFoundError(str(fake))

    monkeypatch.setattr(env_overrides.os.path, "getsize", _gone)
    applied, refused = env_overrides.overrides_from_env_data({KEY: str(fake)})
    assert applied == {} and "cannot be read" in refused[0]


@pytest.mark.skipif(os.name == "nt", reason="Windows has no execute bit")
def test_a_kiro_bin_without_the_execute_bit_is_refused(tmp_path: Path) -> None:
    fake = _fake_bin(tmp_path, executable=False)
    with pytest.raises(env_overrides.EnvOverrideError, match="not an executable file"):
        env_overrides.parse_assignments([f"{KEY}={fake}"])


def test_malformed_and_repeated_entries_are_refused(tmp_path: Path) -> None:
    fake = _fake_bin(tmp_path)
    with pytest.raises(env_overrides.EnvOverrideError, match="expected KEY=VALUE"):
        env_overrides.parse_assignments([KEY])
    with pytest.raises(env_overrides.EnvOverrideError, match="more than once"):
        env_overrides.parse_assignments([f"{KEY}={fake}", f"{KEY}={fake}"])
    with pytest.raises(env_overrides.EnvOverrideError, match="single line"):
        env_overrides.parse_assignments([f"{KEY}={fake}\nPATH=/tmp"])
    with pytest.raises(env_overrides.EnvOverrideError, match="KEY=VALUE strings"):
        env_overrides.parse_assignments([{KEY: str(fake)}])  # type: ignore[list-item]


@pytest.mark.parametrize("value", ["", "plain", "with spaces", "quote'and=equals", "\t"])
def test_single_line_env_values_are_accepted(value: str) -> None:
    assert env_overrides.is_single_line_env_value(value)


@pytest.mark.parametrize("separator", LINE_BREAKS + ("\x00",))
def test_single_line_validator_refuses_separators_and_nul(separator: str) -> None:
    assert not env_overrides.is_single_line_env_value(separator)
    assert not env_overrides.is_single_line_env_value(f"before{separator}after")
    assert not env_overrides.is_single_line_env_value(f"before{separator}")


@pytest.mark.parametrize("separator", LINE_BREAKS + ("\x00",))
def test_parse_assignments_refuses_env_line_injection(tmp_path: Path, separator: str) -> None:
    fake = _fake_bin(tmp_path)
    with pytest.raises(env_overrides.EnvOverrideError, match="must be a single line"):
        env_overrides.parse_assignments([f"{KEY}={fake}{separator}APPROVAL=yolo"])


@pytest.mark.parametrize("separator", LINE_BREAKS + ("\x00",))
def test_boot_recheck_refuses_env_line_injection(tmp_path: Path, separator: str) -> None:
    fake = _fake_bin(tmp_path)
    applied, refused = env_overrides.overrides_from_env_data(
        {KEY: f"{fake}{separator}APPROVAL=yolo"}
    )
    assert applied == {}
    assert refused == [f"--env {KEY}: value must be a single line"]


@pytest.mark.parametrize("separator", LINE_BREAKS + ("\x00",))
@pytest.mark.parametrize("existing", [False, True], ids=["updates", "merged"])
def test_write_env_file_refuses_env_line_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, separator: str, existing: bool
) -> None:
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    cfg = PodConfig.load()
    values = {"SEED": f"/seed{separator}APPROVAL=yolo"}
    if existing:
        monkeypatch.setattr(rt, "read_env_file", lambda cfg, name: values.copy())
    with pytest.raises(rt.PodError, match="must be single-line"):
        rt.write_env_file(cfg, "x", {} if existing else values)
    assert not cfg.env_file("x").exists()


def test_an_empty_value_asks_for_no_override() -> None:
    assert env_overrides.parse_assignments([f"{KEY}="]) == {}


def test_the_parser_collects_each_env_flag() -> None:
    from kiro_crew import cli_parser_admin

    parser = argparse.ArgumentParser()
    cli_parser_admin.register_pod_parser(parser.add_subparsers(dest="command"))
    up = parser.parse_args(["pod", "up", "wt", "--env", f"{KEY}=/a", "--env=X=1"])
    assert up.env == [f"{KEY}=/a", "X=1"]
    assert parser.parse_args(["pod", "up", "wt"]).env is None


def test_an_up_clears_only_an_override_the_pod_already_had(tmp_path: Path) -> None:
    fake = str(_fake_bin(tmp_path))
    assert env_overrides.env_file_updates({}, {}) == {}
    assert env_overrides.env_file_updates({}, {KEY: fake}) == {KEY: ""}
    assert env_overrides.env_file_updates({KEY: fake}, {}) == {KEY: fake}


def test_boot_reads_only_allowed_keys_and_rechecks_them(tmp_path: Path) -> None:
    fake = str(_fake_bin(tmp_path))
    applied, refused = env_overrides.overrides_from_env_data(
        {KEY: fake, "SLACK_BOT_TOKEN": "xoxb-1", "PATH": "/tmp"}
    )
    assert applied == {KEY: fake}
    assert refused == []
    applied, refused = env_overrides.overrides_from_env_data({KEY: "relative/x"})
    assert applied == {}
    assert len(refused) == 1 and "not an absolute path" in refused[0]


# The child-viability probe


def _dead_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.agent_sdk import pod_child_probe

    monkeypatch.setattr(
        pod_child_probe,
        "probe_pod_child_bootstrap",
        lambda env, timeout_secs: pod_child_probe.ProbeResult(
            pod_child_probe.PROBE_DEAD,
            "exited immediately (2): error: unexpected argument '--auth-method'",
            "/abs/fake",
            "/pod/os-home",
        ),
    )


def test_a_dead_test_double_boots_anyway(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    # The double was chosen to reproduce exactly this failure, so refusing the
    # boot would hide the state the operator came to see.
    _dead_probe(monkeypatch)
    runtime_boot._probe_pod_child_bootstrap({}, test_double=True)
    out = capsys.readouterr().out
    assert "--auth-method" in out and "booting anyway" in out


def test_a_dead_real_child_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    _dead_probe(monkeypatch)
    with pytest.raises(rt.PodError, match="every agent turn in this pod"):
        runtime_boot._probe_pod_child_bootstrap({})


# Boot, pod exec and pod up


def _plane(root: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(root / "env"))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(root / "pods"))
    monkeypatch.delenv(KEY, raising=False)
    c = PodConfig.load()
    rt.pin_checkout(c, "x", _ready_worktree(root, "x"))
    return c


def _boot(c: PodConfig, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, dict]:
    """Boot pod ``x``; return (the exec env, the probe's keyword arguments)."""
    probed: dict = {}
    execs: list[dict] = []

    def _probe(pod_env: dict, **kw: object) -> None:
        probed.update(kw, env=dict(pod_env))

    monkeypatch.setattr(runtime_boot, "_probe_pod_child_bootstrap", _probe)
    monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
    monkeypatch.setattr(os, "execve", lambda path, argv, e: execs.append(e))
    rt.boot(c, "x")
    assert len(execs) == 1, "boot did not exec exactly once"
    return execs[0], probed


@requires_posix_pod_lifecycle
def test_boot_hands_the_override_to_the_gateway_and_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _plane(tmp_path, monkeypatch)
    fake = str(_fake_bin(tmp_path))
    rt.write_env_file(c, "x", {KEY: fake})
    env, probed = _boot(c, monkeypatch)
    assert env[KEY] == fake
    assert probed["env"][KEY] == fake
    assert probed["test_double"] is True


@requires_posix_pod_lifecycle
def test_boot_without_an_override_probes_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = _plane(tmp_path, monkeypatch)
    env, probed = _boot(c, monkeypatch)
    assert KEY not in env
    assert probed["test_double"] is False


@requires_posix_pod_lifecycle
def test_boot_drops_a_hand_edited_value_that_fails_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    c = _plane(tmp_path, monkeypatch)
    rt.write_env_file(c, "x", {KEY: "relative/kiro-cli"})
    env, probed = _boot(c, monkeypatch)
    assert KEY not in env
    assert probed["test_double"] is False
    assert "ignoring env override" in capsys.readouterr().out


@requires_posix_pod_lifecycle
def test_pod_exec_sees_the_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = _plane(tmp_path, monkeypatch)
    fake = str(_fake_bin(tmp_path))
    rt.write_env_file(c, "x", {KEY: fake})
    _, env = rt.pod_context(c, "x")
    assert env[KEY] == fake


@requires_posix_pod_lifecycle
class TestPodUp:
    def _prep(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
        monkeypatch.setenv("KIROCREW_POD_WORKTREES_ROOT", str(tmp_path / "wts"))
        monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
        monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
        monkeypatch.setattr(rt, "_git_worktrees", lambda ref: {})
        monkeypatch.setattr(rt, "require_backend", lambda: None)
        _ready_worktree(tmp_path / "wts", "demo")
        monkeypatch.setattr(rt, "derive_port", lambda cfg, n: 7811)
        self.active = False
        monkeypatch.setattr(rt, "is_active", lambda cfg, n: self.active)
        monkeypatch.setattr(rt, "systemctl", lambda *a, **k: _cp(returncode=0))
        monkeypatch.setattr(pod_cli, "_wait_healthy", lambda cfg, n, p, tries=0, **_kw: 403)
        monkeypatch.setattr(rt, "mint_token", lambda cfg, n, ttl: "tok-9")
        self.audits: list[tuple] = []
        monkeypatch.setattr(pod_cli, "_audit", lambda *a, **k: self.audits.append((a, k)))
        return PodConfig.load()

    def _up(self, c: PodConfig, env: list[str] | None) -> None:
        pod_cli._up(
            c,
            argparse.Namespace(
                name="demo",
                json=False,
                seed="",
                ttl="2h",  # flake-ok: the token lifetime string `up` echoes, not a wait
                provision=False,
                env=env,
            ),
        )

    def test_up_records_the_override_and_a_later_up_clears_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        fake = str(_fake_bin(tmp_path))
        self._up(c, [f"{KEY}={fake}"])
        assert rt.read_env_file(c, "demo")[KEY] == fake
        # The audit row names the key, never where it points.
        (row,) = [a[2] for a, _ in self.audits if a[:2] == ("pod.up", "allowed")]
        assert f"env={KEY}" in row and fake not in row
        self._up(c, None)
        assert rt.read_env_file(c, "demo")[KEY] == ""

    def test_up_without_env_writes_no_override_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        self._up(c, None)
        assert KEY not in rt.read_env_file(c, "demo")

    def test_a_refused_key_stops_up_before_anything_changes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        c = self._prep(tmp_path, monkeypatch)
        with pytest.raises(SystemExit):
            self._up(c, ["AWS_SECRET_ACCESS_KEY=abc"])
        assert "not an allowed key" in capsys.readouterr().err
        assert not c.env_file("demo").exists()

    def test_clearing_on_a_running_pod_says_it_applies_next_boot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        # The running gateway keeps the double until it reboots, so the clear
        # must be reported like any other boot-time setting.
        monkeypatch.setattr(rt, "IS_WINDOWS", False)
        c = self._prep(tmp_path, monkeypatch)
        self._up(c, [f"{KEY}={_fake_bin(tmp_path)}"])
        capsys.readouterr()
        self.active = True
        self._up(c, None)
        assert rt.read_env_file(c, "demo")[KEY] == ""
        err = capsys.readouterr().err
        assert "already running" in err and f"--env {KEY}=" in err
        rows = [a[2] for a, _ in self.audits if a[:2] == ("pod.up", "allowed")]
        assert "applied=next_boot" in rows[-1]
