"""Tests for the sandbox guard.

This container runs SANDBOXED-ONLY. kiro-cli runs the model subprocess inside an
unprivileged user namespace; without one, ``wrap_argv`` fails closed. There is no
opt-in to run unsandboxed, because the worker holds the model credential
(``KIRO_API_KEY``) and auto-approves every tool -- an unsandboxed worker on
untrusted prompt content would be a credential-exfiltration path. So on a host
without a user namespace the container refuses to start rather than run the model
subprocess exposed. These pin that: it refuses when the probe reports no
namespace, it refuses when the probe cannot reach an answer at all, and it
proceeds only on a positive verdict.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from container.common import Settings
from container.common.config import ConfigError, _bool
from container.supervisor import __main__ as entry


def make_settings(tmp_path: Path) -> Settings:
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
    )


def test_the_guard_refuses_when_no_user_namespace_is_available(tmp_path: Path) -> None:
    """No sandbox and no opt-in escape: the container must refuse to start.

    This is the whole point of the sandboxed-only posture. There is deliberately
    no config key or env var that lets a deployment run the model subprocess
    unsandboxed, so the only safe answer on a host without a user namespace
    (Fargate today) is to refuse.
    """
    settings = make_settings(tmp_path)
    with pytest.raises(ConfigError, match="sandboxed-only"):
        entry.verify_sandbox(settings, probe=lambda: entry.SANDBOX_DENIED)


def test_the_refusal_names_the_missing_sandbox_not_a_missing_opt_in(tmp_path: Path) -> None:
    """The message must not point the operator at an opt-in that does not exist.

    An unsandboxed posture would tell operators to set
    ``agent.sandbox_allow_unsandboxed_exec``; a message still saying that would
    send them to a dead knob. It must name the missing user namespace instead.
    """
    settings = make_settings(tmp_path)
    with pytest.raises(ConfigError) as exc:
        entry.verify_sandbox(settings, probe=lambda: entry.SANDBOX_DENIED)
    assert "sandbox_allow_unsandboxed_exec" not in str(exc.value)


def test_a_host_with_namespaces_starts(tmp_path: Path) -> None:
    entry.verify_sandbox(make_settings(tmp_path), probe=lambda: entry.SANDBOX_AVAILABLE)


def test_an_undetermined_probe_refuses(tmp_path: Path) -> None:
    """A probe that could not reach an answer must refuse, not proceed.

    A host where the probe cannot run is not a host where the sandbox is known to be
    missing, and treating that as permission to continue is the same defect as reading
    the backend environment through a denylist: it holds for the cases someone already
    enumerated and fails OPEN on the next one. Failing open here means booting a worker
    that auto-approves every tool and holds the model credential with no evidence that a
    sandbox exists, so undetermined refuses exactly as a denial does.
    """
    verdict = f"{entry.SANDBOX_UNDETERMINED_PREFIX}the probe could not fork a child"
    with pytest.raises(ConfigError, match="could not be determined"):
        entry.verify_sandbox(make_settings(tmp_path), probe=lambda: verdict)


def test_an_undetermined_refusal_repeats_what_could_not_be_determined(tmp_path: Path) -> None:
    """An operator needs the specific reason, not just that there was one.

    'Something went wrong with the sandbox probe' is unactionable; 'this platform has
    no os.unshare' and 'the probe child was killed by signal 9' lead to different
    fixes. The verdict is carried into the refusal verbatim so the message names
    which one it was.
    """
    verdict = (
        f"{entry.SANDBOX_UNDETERMINED_PREFIX}the probe child was killed by signal 9 "
        "before it could answer"
    )
    with pytest.raises(ConfigError) as exc:
        entry.verify_sandbox(make_settings(tmp_path), probe=lambda: verdict)
    assert "killed by signal 9" in str(exc.value)


def test_an_unrecognised_verdict_refuses(tmp_path: Path) -> None:
    """The guard fails closed on any verdict it does not know.

    Only ``SANDBOX_AVAILABLE`` proceeds. A verdict added later, a typo, or a stubbed
    probe returning something else entirely all land on the refusal, so extending the
    probe cannot accidentally open the gate -- the direction a security guard must
    fail in when someone adds a case and forgets this call site.
    """
    for bogus in ("AVAILABLE", "yes", "", None, True):
        with pytest.raises(ConfigError):
            entry.verify_sandbox(make_settings(tmp_path), probe=lambda value=bogus: value)


def test_the_real_probe_returns_a_verdict_this_guard_understands() -> None:
    """The shipped probe and the guard must not drift apart.

    Both halves are in this module, and the guard now treats an unknown verdict as a
    refusal -- which means a probe that started returning something else would make
    the container refuse to boot everywhere rather than fail a test. Pin the contract
    here: whatever this host is, the real probe's answer is one the guard recognises.
    """
    verdict = entry._user_namespaces_available()
    assert verdict in (entry.SANDBOX_AVAILABLE, entry.SANDBOX_DENIED) or verdict.startswith(
        entry.SANDBOX_UNDETERMINED_PREFIX
    )


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


@pytest.mark.parametrize("raw", ["ture", "${SinglePrincipal}", "maybe", "2"])
def test_bool_refuses_a_value_it_cannot_read(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """Reading a typo as "no" is the safe direction but hides a broken deployment.

    An unresolved CloudFormation reference is the realistic case: it would leave
    the setting at its safe default with nothing pointing at the parameter that
    failed to resolve.
    """
    monkeypatch.setenv("SMC_PROBE_BOOL", raw)
    with pytest.raises(ConfigError, match="must be a boolean"):
        _bool("SMC_PROBE_BOOL", False)
