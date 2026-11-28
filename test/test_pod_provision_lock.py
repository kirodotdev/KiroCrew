"""Provision transactions exclude contenders without locking pod lifecycle work."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import Mock

import pytest

from kiro_crew import platform_compat
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import runtime as rt
from kiro_crew.pod.config import PodConfig


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PodConfig:
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "env"))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setattr(pod_cli, "_resolve_or_die", lambda c, n: tmp_path / n)
    monkeypatch.setattr(pod_cli, "_require_up_user_bus", lambda n: None)
    return PodConfig.load()


@pytest.mark.parametrize("verb", ["_provision", "_up"])
def test_held_lock_refuses_before_provision(
    cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, capsys, verb: str
) -> None:
    audit = Mock()
    provision = Mock(side_effect=AssertionError("a contending provision must not run"))
    monkeypatch.setattr(pod_cli, "_audit", audit)
    monkeypatch.setattr(pod_cli.prov, "provision", provision)
    args = argparse.Namespace(name="demo", seed="", provision=True)

    with pod_cli._provision_lock(cfg, "demo"):
        with pytest.raises(SystemExit) as exc:
            getattr(pod_cli, verb)(cfg, args)

    message = (
        "another provision of demo is already running; wait for it to finish, then run it again"
    )
    assert exc.value.code == 1
    assert capsys.readouterr().err == f"pod: {message}\n"
    audit.assert_called_once_with("pod.provision", "denied", "name=demo", error=message)
    provision.assert_not_called()


@pytest.mark.parametrize("venv_only", [False, True])
def test_free_lock_spans_provision_and_preserves_pin(
    cfg: PodConfig, monkeypatch: pytest.MonkeyPatch, venv_only: bool
) -> None:
    lock_file = cfg.pods_dir / "demo.provision.lock"

    def provision(checkout: Path, *, build: bool) -> bool:
        assert checkout == cfg.pod_root.parent / "demo"
        assert build is not venv_only
        with platform_compat.open_lock_file(lock_file) as fd:
            with pytest.raises(BlockingIOError):
                with platform_compat.file_lock(fd, exclusive=True, wait=False):
                    pytest.fail("provision must hold its lock throughout the build")
        # Lifecycle work must remain independent of the slow provision lock.
        with rt.pod_name_mutex(cfg, "demo"):
            pass
        return True

    pin = Mock()
    monkeypatch.setattr(pod_cli.prov, "provision", provision)
    monkeypatch.setattr(rt, "pin_checkout", pin)
    pod_cli._provision(cfg, argparse.Namespace(name="demo", venv_only=venv_only))
    pin.assert_called_once_with(cfg, "demo", cfg.pod_root.parent / "demo")
    assert lock_file.exists()
    with pod_cli._provision_lock(cfg, "demo"):
        pass


def test_failed_provision_releases_lock_without_pinning(
    cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin = Mock()
    monkeypatch.setattr(rt, "pin_checkout", pin)
    monkeypatch.setattr(pod_cli.prov, "provision", lambda checkout, build: False)
    with pytest.raises(SystemExit):
        pod_cli._provision(cfg, argparse.Namespace(name="demo"))
    pin.assert_not_called()
    with pod_cli._provision_lock(cfg, "demo"):
        pass


def test_build_blocking_io_error_is_not_reported_as_lock_contention(
    cfg: PodConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    audit = Mock()
    monkeypatch.setattr(pod_cli, "_audit", audit)
    with pytest.raises(BlockingIOError, match="build I/O failed"):
        with pod_cli._provision_lock(cfg, "demo"):
            raise BlockingIOError("build I/O failed")
    audit.assert_not_called()
    with pod_cli._provision_lock(cfg, "demo"):
        pass


def test_different_names_can_provision_together(cfg: PodConfig) -> None:
    with pod_cli._provision_lock(cfg, "demo"):
        with pod_cli._provision_lock(cfg, "other"):
            pass
