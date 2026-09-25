"""Tests for the sandbox guard.

kiro-cli runs the model subprocess inside an unprivileged user namespace; without
one, ``wrap_argv`` fails closed. This container is sandboxed-only, so a host that
cannot provide one is refused at startup rather than left to fail every turn.

Taking the model credential out of the worker's environment does not change that, and
these pin the distinction. What matters for an auto-approved worker on untrusted
prompt content is whether it can REACH a credential, not whether one is resident in
its own environment. ``build_backend_env`` closes the environment route, and the guard
ASSERTS that -- refusing on any verdict, because a value there is a broken invariant
rather than a property of the host. The route that stays open is the vault: the backend
answers the engine's token request from it under a uid the worker shares, so no
refusal here can be traded away for a clean environment.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from container.common import Settings
from container.common.config import ConfigError, _bool
from container.supervisor import __main__ as entry

#: One delivered model identity, in the shape Secrets Manager carries it.
IDENTITY_JSON = json.dumps(
    {
        "access_token": "atk-test",
        "expires_at": "2099-01-01T00:00:00+00:00",
        "provider": "BuilderId",
        "identity": "builder_id",
    }
)


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
    """Sandboxed-only: a host that cannot sandbox the worker is refused at startup.

    Refused here rather than left to fail every turn, because a container that answers
    its port and fails each turn looks healthy and is not.
    """
    with pytest.raises(ConfigError, match="sandboxed-only"):
        entry.verify_sandbox(make_settings(tmp_path), env={}, probe=lambda: entry.SANDBOX_DENIED)


def test_the_refusal_says_why_removing_the_credential_is_not_enough(tmp_path: Path) -> None:
    """The message has to answer the question an operator will actually ask.

    "The credential is not in the worker's environment any more, so why refuse?" ---
    because the backend answers the engine's token request from the vault, so the
    backend's uid must be able to decrypt it, and the worker is a child of the backend
    under that same uid. A refusal that named only the missing sandbox would send them
    looking for a config switch that must not exist.
    """
    with pytest.raises(ConfigError) as exc:
        entry.verify_sandbox(make_settings(tmp_path), env={}, probe=lambda: entry.SANDBOX_DENIED)
    message = str(exc.value)
    assert "vault" in message
    assert "same" in message and "uid" in message


@pytest.mark.parametrize("name", ["KIRO_IDENTITY", "KIRO_API_KEY"])
def test_a_credential_in_the_backends_environment_refuses_whatever_the_sandbox_says(
    tmp_path: Path, name: str
) -> None:
    """An ASSERTION, not a posture: it fires even with a sandbox available.

    ``build_backend_env`` withholding the credential is something this code controls,
    so a value in ``env`` means that withholding was removed or defeated. That is a
    broken invariant rather than a property of the host, which is why it is checked
    before the probe runs and refuses on every verdict.

    Both shapes, because covering one and not its sibling would leave the same path
    open beside the guard.
    """
    for verdict in (entry.SANDBOX_AVAILABLE, entry.SANDBOX_DENIED):
        with pytest.raises(ConfigError, match="build_backend_env"):
            entry.verify_sandbox(
                make_settings(tmp_path), env={name: "sk-live"}, probe=lambda v=verdict: v
            )


def test_a_blank_credential_value_is_not_a_credential(tmp_path: Path) -> None:
    """The assertion's condition is a readable value, not the presence of the name.

    An empty or whitespace variable reaches the worker as nothing at all, so treating
    it as a broken invariant would refuse a container that is in fact intact.
    """
    for blank in ("", "   ", "\t"):
        entry.verify_sandbox(
            make_settings(tmp_path),
            env={"KIRO_API_KEY": blank},
            probe=lambda: entry.SANDBOX_AVAILABLE,
        )


def test_a_host_with_namespaces_starts(tmp_path: Path) -> None:
    entry.verify_sandbox(make_settings(tmp_path), env={}, probe=lambda: entry.SANDBOX_AVAILABLE)


def test_an_undetermined_probe_refuses(tmp_path: Path) -> None:
    """A probe that could not reach an answer must refuse, not proceed.

    A host where the probe cannot run is not a host where the sandbox is known to be
    missing, and treating that as permission to continue is the same defect as reading
    the backend environment through a denylist: it holds for the cases someone already
    enumerated and fails OPEN on the next one.
    """
    verdict = f"{entry.SANDBOX_UNDETERMINED_PREFIX}the probe could not fork a child"
    with pytest.raises(ConfigError, match="could not be determined"):
        entry.verify_sandbox(make_settings(tmp_path), env={}, probe=lambda: verdict)


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
        entry.verify_sandbox(make_settings(tmp_path), env={}, probe=lambda: verdict)
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
            entry.verify_sandbox(make_settings(tmp_path), env={}, probe=lambda value=bogus: value)


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
