"""A cli.sh re-run under a running gateway names the command that restarts it.

Both install branches write the new version into the tree the running gateway
was loaded from, so that gateway runs a mix of old and new code until it is
restarted. The installer never restarts it: it finds a running gateway with
read-only probes (``systemctl show``, ``launchctl print``, ``ps``) and
ends with a warning that names the restart command for how that gateway runs.

The cases drive the real script through the signed-manifest harness of
``test_installer_binary_only``, with recording stubs for ``systemctl``,
``launchctl`` and ``ps`` placed ahead of the system directories on the closed
``PATH``, so no host service manager is ever asked anything.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from installer_test_helpers import run_bounded
from test_cli_manifest_signature import (  # noqa: F401  (fixtures are looked up by name)
    SigningKey,
    _openssl_bin,
    test_key,
)
from test_installer_binary_only import _existing_managed_venv, _prepare_installer

WARNING = "WARNING: a Kiro Crew gateway is running and still has the previous version loaded."

# Each stub records its argv and answers from the environment.
# FAKE_SYSTEM_ACTIVE / FAKE_USER_ACTIVE: 1 = our unit is active; "alias" =
# the name is an alias of another, active unit (the manager reports that
# unit's Id). Anything else: no unit by that name in the scope.
_SYSTEMCTL = """#!/bin/sh
printf 'systemctl %s\\n' "$*" >> "$FAKE_SVC_LOG"
case "$*" in
  "show -p Id -p ActiveState kirocrew.service") state="${FAKE_SYSTEM_ACTIVE:-0}" ;;
  "--user show -p Id -p ActiveState kirocrew.service") state="${FAKE_USER_ACTIVE:-0}" ;;
  *) exit 1 ;;
esac
case "$state" in
  1) printf 'Id=kirocrew.service\\nActiveState=active\\n' ;;
  alias) printf 'Id=shared.service\\nActiveState=active\\n' ;;
  *) printf 'Id=kirocrew.service\\nActiveState=inactive\\n' ;;
esac
exit 0
"""

_LAUNCHCTL = """#!/bin/sh
printf 'launchctl %s\\n' "$*" >> "$FAKE_SVC_LOG"
[ "$1" = print ] && [ "${FAKE_LAUNCHD_LOADED:-0}" = 1 ] && exit 0
exit 113
"""

_PS = """#!/bin/sh
printf 'ps %s\\n' "$*" >> "$FAKE_SVC_LOG"
[ -n "${FAKE_PS_ARGS:-}" ] && printf '%s\\n' "$FAKE_PS_ARGS"
exit 0
"""

# The only argv shapes the probes may send: every one is a read.
_READ_ONLY_CALLS = {
    "systemctl show -p Id -p ActiveState kirocrew.service",
    "systemctl --user show -p Id -p ActiveState kirocrew.service",
    f"launchctl print gui/{getattr(os, 'getuid', lambda: 0)()}/dev.kirocrew.gateway",
}


@pytest.fixture(scope="module")
def signing_key(test_key: SigningKey) -> SigningKey:  # noqa: F811 -- pytest fixture by name
    if os.name == "nt":
        pytest.skip("cli.sh is supported on macOS and Linux only")
    return test_key


def _run(
    case: Path,
    key: SigningKey,
    *,
    with_pipx: bool = True,
    existing: bool = True,
    lock_pid: str | None = None,
    env_extra: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    if not with_pipx and existing:
        _existing_managed_venv(case)
    argv, env, run_root, _argv_file = _prepare_installer(
        case, key, with_pipx=with_pipx, existing_pipx_venv=with_pipx and existing
    )
    stubs = run_root / "svc-stubs"
    stubs.mkdir()
    for name, body in (("systemctl", _SYSTEMCTL), ("launchctl", _LAUNCHCTL), ("ps", _PS)):
        stub = stubs / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    log = run_root / "svc-calls"
    env["PATH"] = os.pathsep.join([str(stubs), env["PATH"]])
    env["FAKE_SVC_LOG"] = str(log)
    if lock_pid is not None:
        data_home = Path(env["KIROCREW_HOME"])
        data_home.mkdir(parents=True, exist_ok=True)
        (data_home / "gateway.lock").write_text(f"{lock_pid}\n", encoding="utf-8")
    if env_extra:
        env.update(env_extra)
    result = run_bounded([*argv, "--no-kas"], env, cwd=str(run_root))
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


def _assert_only_reads(calls: list[str]) -> None:
    for call in calls:
        assert call in _READ_ONLY_CALLS or call.startswith("ps -p "), call


_EXPECTED = {
    "systemd-user": "systemctl --user restart kirocrew",
    "systemd-system": "sudo systemctl restart kirocrew",
    "systemd-both": "kirocrew restart",
    "launchd": "launchctl kickstart -k gui/{uid}/dev.kirocrew.gateway",
}


@pytest.mark.parametrize(
    ("setup", "env_extra"),
    [
        ("systemd-user", {"FAKE_USER_ACTIVE": "1"}),
        ("systemd-system", {"FAKE_SYSTEM_ACTIVE": "1"}),
        ("systemd-both", {"FAKE_SYSTEM_ACTIVE": "1", "FAKE_USER_ACTIVE": "1"}),
        ("launchd", {"FAKE_LAUNCHD_LOADED": "1"}),
    ],
    ids=list(_EXPECTED),
)
def test_rerun_under_a_running_service_names_its_restart_command(
    tmp_path: Path,
    signing_key: SigningKey,
    setup: str,
    env_extra: dict[str, str],
) -> None:
    expected = _EXPECTED[setup].format(uid=os.getuid())
    if setup == "systemd-system" and os.getuid() == 0:
        expected = "systemctl restart kirocrew"
    result, calls = _run(tmp_path / "case", signing_key, env_extra=env_extra)

    assert result.returncode == 0, result.stderr
    assert WARNING in result.stderr
    assert f"  {expected}\n" in result.stderr
    # The hint closes the run, after the install summary.
    assert "Installed kirocrew" in result.stdout
    _assert_only_reads(calls)


def test_rerun_of_the_managed_venv_names_the_restart_command(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    result, calls = _run(
        tmp_path / "case", signing_key, with_pipx=False, env_extra={"FAKE_USER_ACTIVE": "1"}
    )

    assert result.returncode == 0, result.stderr
    assert WARNING in result.stderr
    assert "  systemctl --user restart kirocrew\n" in result.stderr
    _assert_only_reads(calls)


@pytest.mark.parametrize(
    "ps_args",
    [
        "/srv/kc/.local/bin/kirocrew gateway",
        "/srv/kc/.local/bin/kirocrew -v gateway --no-open",
        "/srv/kc/venv/bin/python -m kiro_crew --verbose gateway",
    ],
    ids=["plain", "global-flag", "module"],
)
def test_rerun_under_a_foreground_gateway_names_kirocrew_restart(
    tmp_path: Path, signing_key: SigningKey, ps_args: str
) -> None:
    result, calls = _run(
        tmp_path / "case",
        signing_key,
        lock_pid="4242",
        env_extra={"FAKE_PS_ARGS": ps_args},
    )

    assert result.returncode == 0, result.stderr
    assert WARNING in result.stderr
    assert "  kirocrew restart\n" in result.stderr
    assert "ps -p 4242 -o args=" in calls
    _assert_only_reads(calls)


@pytest.mark.parametrize(
    "env_extra",
    [{"FAKE_SYSTEM_ACTIVE": "alias"}, {"FAKE_USER_ACTIVE": "alias"}],
    ids=["system-alias", "user-alias"],
)
def test_an_alias_of_another_unit_gets_no_restart_command(
    tmp_path: Path, signing_key: SigningKey, env_extra: dict[str, str]
) -> None:
    """``kirocrew.service`` aliasing another active unit is not this gateway: a
    command naming kirocrew would restart that other unit."""
    result, calls = _run(tmp_path / "case", signing_key, env_extra=env_extra)

    assert result.returncode == 0, result.stderr
    assert WARNING not in result.stderr
    assert "systemctl" not in result.stderr
    _assert_only_reads(calls)


@pytest.mark.parametrize(
    ("lock_pid", "ps_args"),
    [
        ("4242", "/usr/bin/sleep 100"),
        ("4242", "vim /srv/kc/kirocrew-gateway.log"),
        ("4242", ""),
        ("not-a-pid", "kirocrew gateway"),
        (None, ""),
    ],
    ids=[
        "stale-pid-other-process",
        "stale-pid-lookalike",
        "stale-pid-gone",
        "garbled-lock",
        "no-lock",
    ],
)
def test_rerun_with_no_running_gateway_prints_no_hint(
    tmp_path: Path, signing_key: SigningKey, lock_pid: str | None, ps_args: str
) -> None:
    result, calls = _run(
        tmp_path / "case", signing_key, lock_pid=lock_pid, env_extra={"FAKE_PS_ARGS": ps_args}
    )

    assert result.returncode == 0, result.stderr
    assert WARNING not in result.stderr
    _assert_only_reads(calls)


def test_retiring_the_legacy_in_data_home_venv_names_the_restart_command(
    tmp_path: Path, signing_key: SigningKey
) -> None:
    """A first managed-venv build that retires the legacy ``<data home>/venv``
    deletes the tree a running gateway was loaded from, so it warns too."""
    case = tmp_path / "case"
    legacy = case / "run" / "data-home" / "venv"
    (legacy / "bin").mkdir(parents=True)
    (legacy / "pyvenv.cfg").write_text("home = /legacy\n", encoding="utf-8")
    (legacy / "bin" / "kirocrew").write_text("#!/bin/sh\necho legacy\n", encoding="utf-8")
    result, calls = _run(
        case, signing_key, with_pipx=False, existing=False, env_extra={"FAKE_USER_ACTIVE": "1"}
    )

    assert result.returncode == 0, result.stderr
    assert not legacy.exists()
    assert WARNING in result.stderr
    assert "  systemctl --user restart kirocrew\n" in result.stderr
    _assert_only_reads(calls)


def test_first_install_prints_no_hint(tmp_path: Path, signing_key: SigningKey) -> None:
    """A first install replaced nothing a gateway could have loaded."""
    result, calls = _run(
        tmp_path / "case", signing_key, existing=False, env_extra={"FAKE_USER_ACTIVE": "1"}
    )

    assert result.returncode == 0, result.stderr
    assert WARNING not in result.stderr
    assert calls == []
