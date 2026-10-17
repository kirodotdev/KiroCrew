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

One thing DOES lift the refusal, and it is a trust boundary rather than a credential
claim: ``settings.internal_only``, the deployment stating that this task runs the
operator's OWN crews and that the operator bears the risk of what those crews read. The
exposure is then accepted, and it is bigger than the setting's name suggests -- untrusted
CONTENT the crew reads in the ordinary course of its work (tool output, a fetched page, a
connector payload) can inject the unsandboxed worker whoever sent the prompt, and that
worker can read the vault. What the claim buys is that the credential at risk is the
operator's own, not that injection cannot happen.
The tests at the end of this file are mostly about the limits of that: it lifts a
DENIED verdict and nothing else, and it never lifts the credential assertion.
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


def make_settings(tmp_path: Path, *, internal_only: bool = False) -> Settings:
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
        internal_only=internal_only,
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


# ── The internal-only boundary ──
#
# The Fargate lane runs the operator's own crews, and the operator bears the risk of what
# those crews read. Under that claim an unsandboxed model subprocess is ACCEPTED rather
# than fixed, which is what lets the container start on a host with no user namespace --
# every Fargate host, measured on a real task. These pin what the claim does and, more
# importantly, what it does not.


def test_a_denied_host_starts_when_the_deployment_claims_internal_only(tmp_path: Path) -> None:
    """The one behaviour the claim buys: a start where the guard otherwise refuses.

    Same host, same verdict, same clean environment as
    ``test_the_guard_refuses_when_no_user_namespace_is_available`` one screen up -- the
    claim is the only thing that differs, which is what makes this a test about the claim
    rather than about the probe.
    """
    entry.verify_sandbox(
        make_settings(tmp_path, internal_only=True),
        env={},
        probe=lambda: entry.SANDBOX_DENIED,
    )


def test_the_claim_does_not_lift_an_undetermined_verdict(tmp_path: Path) -> None:
    """A boundary can accept a KNOWN exposure. It cannot accept an unknown one.

    ``SANDBOX_DENIED`` is a definite answer about the host, so there is something for the
    claim to accept: no user namespace here, and the consequence is written down. An
    undetermined verdict carries no answer at all -- the probe could not run -- so there
    is nothing to accept and the refusal stands whatever the deployment claims.

    Stated as a test because the comfortable reading of "unsandboxed is accepted" is that
    the sandbox verdict stops mattering, and that reading turns a broken probe into a
    silent start.
    """
    verdict = f"{entry.SANDBOX_UNDETERMINED_PREFIX}the probe could not fork a child"
    with pytest.raises(ConfigError, match="could not be determined"):
        entry.verify_sandbox(
            make_settings(tmp_path, internal_only=True), env={}, probe=lambda: verdict
        )


def test_the_claim_does_not_lift_an_unrecognised_verdict(tmp_path: Path) -> None:
    """The guard still fails closed on a verdict it does not know, claim or no claim.

    Only the two verdicts the guard recognises get a decision. A probe extended later, a
    typo, or a stub returning something else must not reach the loosened posture by way of
    the claim -- that is the direction a security guard fails in when someone adds a case
    and forgets this call site.
    """
    for bogus in ("AVAILABLE", "yes", "", None, True):
        with pytest.raises(ConfigError):
            entry.verify_sandbox(
                make_settings(tmp_path, internal_only=True),
                env={},
                probe=lambda value=bogus: value,
            )


@pytest.mark.parametrize("name", ["KIRO_IDENTITY", "KIRO_API_KEY"])
def test_a_credential_in_the_environment_refuses_even_under_the_claim(
    tmp_path: Path, name: str
) -> None:
    """The claim is about a HOST POSTURE. The credential check is about a broken invariant.

    ``build_backend_env`` popping both names is something this container controls, so a
    value in ``env`` means that withholding was removed or defeated. No trust boundary
    makes that acceptable: the deployment vouched for who sends prompts, not for the
    container's own code being intact.
    """
    with pytest.raises(ConfigError, match="build_backend_env"):
        entry.verify_sandbox(
            make_settings(tmp_path, internal_only=True),
            env={name: "sk-live"},
            probe=lambda: entry.SANDBOX_DENIED,
        )


def test_the_refusal_names_the_setting_and_what_accepting_it_means(tmp_path: Path) -> None:
    """An operator refused on Fargate must be told the decision, not sent to another host.

    "Run where unprivileged user namespaces are permitted" is advice with no action behind
    it in this lane -- there is no such Fargate host. So the refusal names
    ``SMC_INTERNAL_ONLY`` and states what claiming it accepts: an auto-approving worker
    that can reach the model credential, acceptable only because no external party sends
    prompts. Naming the setting without the consequence would be an invitation to set it.
    """
    with pytest.raises(ConfigError) as exc:
        entry.verify_sandbox(make_settings(tmp_path), env={}, probe=lambda: entry.SANDBOX_DENIED)
    message = str(exc.value)
    assert "SMC_INTERNAL_ONLY" in message
    lowered = message.lower()
    assert "own crews" in lowered, "the refusal does not say what the boundary is"
    assert "auto-approves" in lowered, "the refusal does not say what accepting it accepts"


def test_the_undetermined_refusal_says_the_claim_is_not_the_fix_for_it(tmp_path: Path) -> None:
    """The message has to close the door the previous one opens.

    An operator who has just read "set SMC_INTERNAL_ONLY=1" on a denied host will try it
    on an undetermined one. Saying only "could not be determined" leaves them setting the
    flag and watching nothing change, with no hint that the probe itself is the problem.
    """
    verdict = f"{entry.SANDBOX_UNDETERMINED_PREFIX}the probe could not fork a child"
    with pytest.raises(ConfigError) as exc:
        entry.verify_sandbox(
            make_settings(tmp_path, internal_only=True), env={}, probe=lambda: verdict
        )
    assert "SMC_INTERNAL_ONLY does not cover this" in str(exc.value)


def test_the_claim_is_read_from_the_environment_under_its_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``load()`` must read ``SMC_INTERNAL_ONLY``, and absent must mean not claimed.

    The chain is operator file -> derived task variable -> this setting -> the guard's
    branch, and every link is asserted somewhere. This is the link between the variable
    the launcher writes and the field the guard reads: without it the whole chain could be
    wired and the container would still never see the claim.
    """
    from container.common.config import load

    monkeypatch.delenv("SMC_INTERNAL_ONLY", raising=False)
    assert load().internal_only is False, "absent must read as not claimed"
    monkeypatch.setenv("SMC_INTERNAL_ONLY", "1")
    assert load().internal_only is True
    monkeypatch.setenv("SMC_INTERNAL_ONLY", "false")
    assert load().internal_only is False


def test_an_unreadable_claim_is_refused_and_names_what_it_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not read as "not claimed" silently, and the refusal must say which setting.

    An unresolved template reference is the realistic case. Refusing in the safe direction
    is still refusing: the deployment did not say what it meant about a boundary that
    decides whether the model subprocess runs sandboxed, so the operator fixes the value.
    The message names the sandbox consequence rather than the single-principal one, which
    is what a shared sentence for both booleans would have got wrong.
    """
    from container.common.config import load

    monkeypatch.setenv("SMC_INTERNAL_ONLY", "${InternalOnly}")
    with pytest.raises(ConfigError, match="SMC_INTERNAL_ONLY must be a boolean") as exc:
        load()
    assert "unsandboxed" in str(exc.value)
