"""Tests for the sandbox posture seam.

The failure these cover was found by deploying: on Fargate the container refused
to start because ``agent.sandbox_allow_unsandboxed_exec`` was off, and the message
named a ``config.json`` that nothing in the repository ever wrote. The guard read
the key and the deployment never set it, so each side had correctly left the
security decision to the other and neither made it.

The case that matters most here is the LAST one: with the posture applied, the
guard that refused to boot now passes. A test that only checks the file contents
would not have caught a key written under the wrong name or in the wrong file.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from container.common import Settings
from container.common.config import ConfigError, _bool
from container.supervisor import __main__ as entry


def make_settings(tmp_path: Path, *, allow: bool) -> Settings:
    data_home = tmp_path / "data"
    data_home.mkdir(parents=True, exist_ok=True)
    return Settings(
        backend_port=8765,
        backend_run_dir=data_home / "run",
        front_port=8080,
        route_prefix="",
        control_secret=None,
        data_home=data_home,
        config_dir=data_home,
        crew_name="test-crew",
        backup_bucket=None,
        backup_prefix="",
        backup_interval_secs=30,
        allow_unsandboxed_exec=allow,
    )


def test_the_default_posture_is_the_safe_one() -> None:
    """A Settings that does not name the field must not permit unsandboxed exec."""
    fields = {f.name: f for f in dataclasses.fields(Settings)}
    assert fields["allow_unsandboxed_exec"].default is False


def test_posture_writes_the_key_the_guard_reads(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow=True)
    entry.apply_sandbox_posture(settings)
    written = json.loads((settings.data_home / "config.json").read_text())
    assert written["agent"]["sandbox_allow_unsandboxed_exec"] is True


def test_posture_writes_nothing_when_not_permitted(tmp_path: Path) -> None:
    """The safe posture must leave no file, so it cannot be mistaken for consent."""
    settings = make_settings(tmp_path, allow=False)
    entry.apply_sandbox_posture(settings)
    assert not (settings.data_home / "config.json").exists()


def test_posture_merges_and_keeps_every_other_key(tmp_path: Path) -> None:
    """Replacing the file would silently discard settings that matter."""
    settings = make_settings(tmp_path, allow=True)
    path = settings.data_home / "config.json"
    path.write_text(
        json.dumps(
            {
                "telemetry": {"beacon_enabled": False},
                "agent": {"model": "some-model"},
            }
        )
    )

    entry.apply_sandbox_posture(settings)

    written = json.loads(path.read_text())
    assert written["telemetry"]["beacon_enabled"] is False
    assert written["agent"]["model"] == "some-model"
    assert written["agent"]["sandbox_allow_unsandboxed_exec"] is True


def test_posture_refuses_an_unparseable_config_rather_than_replacing_it(
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path, allow=True)
    path = settings.data_home / "config.json"
    path.write_text("{ this is not json")

    with pytest.raises(ConfigError, match="not readable JSON"):
        entry.apply_sandbox_posture(settings)

    assert path.read_text() == "{ this is not json"


def test_posture_does_not_rewrite_a_file_already_saying_yes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow=True)
    path = settings.data_home / "config.json"
    entry.apply_sandbox_posture(settings)
    before = path.stat().st_mtime_ns

    entry.apply_sandbox_posture(settings)

    assert path.stat().st_mtime_ns == before


def test_the_guard_still_refuses_without_the_posture(tmp_path: Path) -> None:
    """The deployed failure, reproduced: no user namespace and no opt-in."""
    settings = make_settings(tmp_path, allow=False)
    entry.apply_sandbox_posture(settings)

    with pytest.raises(ConfigError, match="sandbox_allow_unsandboxed_exec is off"):
        entry.verify_sandbox(settings, probe=lambda: False)


def test_the_posture_makes_the_guard_pass(tmp_path: Path) -> None:
    """The fix, end to end: same host, same guard, now boots.

    This is the assertion that ties the write to the read. It fails if the key is
    written under a different name, nested differently, or into another file --
    each of which a contents-only test would accept.
    """
    settings = make_settings(tmp_path, allow=True)
    entry.apply_sandbox_posture(settings)

    entry.verify_sandbox(settings, probe=lambda: False)  # must not raise


def test_a_host_with_namespaces_needs_no_posture(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, allow=False)
    entry.verify_sandbox(settings, probe=lambda: True)


def test_an_unknown_probe_result_does_not_block(tmp_path: Path) -> None:
    """Non-Linux: the probe cannot run, so it must not be read as unavailable."""
    settings = make_settings(tmp_path, allow=False)
    entry.verify_sandbox(settings, probe=lambda: None)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
        (" true ", True),
    ],
)
def test_bool_accepts_the_spellings_a_template_may_produce(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
) -> None:
    monkeypatch.setenv("SMC_PROBE_BOOL", raw)
    assert _bool("SMC_PROBE_BOOL", False) is expected


@pytest.mark.parametrize("raw", ["ture", "${AllowUnsandboxedExec}", "maybe", "2"])
def test_bool_refuses_a_value_it_cannot_read(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Reading a typo as "no" is the safe direction but hides a broken deployment.

    An unresolved CloudFormation reference is the realistic case: it would leave
    the sandbox on and the container refusing to boot, with nothing pointing at
    the parameter that failed to resolve.
    """
    monkeypatch.setenv("SMC_PROBE_BOOL", raw)
    with pytest.raises(ConfigError, match="must be a boolean"):
        _bool("SMC_PROBE_BOOL", False)
