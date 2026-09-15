"""``kirocrew doctor`` reports Claude Code as a real optional agent backend.

The old behaviour printed the adapter ONLY when it happened to be present, and
labelled it "dormant seam -- not used by the public core". Both halves were wrong once
``ACP_BACKEND_CLAUDE`` joined ``BASELINE_SELECTABLE_BACKENDS``: an operator who could
select the harness was told the build did not use it, and an operator who had NOT
installed the adapter was told nothing at all -- doctor's whole job is naming the thing
that is absent.

These tests call ``_doctor_claude_backend`` rather than ``_doctor()``, and stub the
probe. The full doctor shells out to ``kiro-cli whoami`` and walks the host, so
invoking it here would reach the real installation of whoever runs the suite; and a
probe left unstubbed would make the assertions depend on which binaries that machine
happens to have.
"""

import contextlib
import io

import pytest

from kiro_crew import cli_doctor
from kiro_crew.agent_sdk import INSTALLED, MISSING, UNKNOWN, BackendInstallState


def _state(installed: str, **over) -> BackendInstallState:
    return BackendInstallState(
        backend="claude",
        policy_id="claude",
        installed=installed,
        **over,
    )


def _report(monkeypatch, state, *, on_path: bool = True) -> str:
    """Run the reporting block with the probe stubbed, capturing stdout.

    ``state=None`` stands for a probe that raised: the function catches it and must
    still print a line rather than staying silent.

    ``on_path=False`` is the case a plain PATH lookup cannot see: the probe resolved
    the adapter through ``CLAUDE_AGENT_ACP_BIN``, a vendored ``node_modules`` or a
    ``mise`` shim, so ``shutil.which`` returns ``None`` for a WORKING install.
    """

    def _probe(_backend):
        if state is None:
            raise RuntimeError("probe blew up")
        return state

    monkeypatch.setattr("kiro_crew.agent_sdk.probe_backend", _probe)
    monkeypatch.setattr(
        cli_doctor.shutil,
        "which",
        lambda _name: "/usr/local/bin/claude-agent-acp" if on_path else None,
    )

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        with contextlib.suppress(SystemExit):
            cli_doctor._doctor_claude_backend()
    return buf.getvalue()


def test_an_install_resolved_off_path_is_not_printed_as_none(monkeypatch):
    """``which`` misses the resolvers the probe honours, so its ``None`` must not print.

    The probe resolves through the spawn's own resolver -- ``CLAUDE_AGENT_ACP_BIN``, a
    vendored ``node_modules``, a ``mise`` shim -- while ``shutil.which`` only sees plain
    PATH. Interpolating that miss produced "✅ None" for a working install, which reads
    as a broken probe rather than a resolved one.
    """
    out = _report(monkeypatch, _state(INSTALLED), on_path=False)
    assert "None" not in out
    assert "installed" in out


def test_a_path_install_still_names_the_location(monkeypatch):
    """The location is useful when we genuinely have it -- do not drop it for everyone."""
    out = _report(monkeypatch, _state(INSTALLED), on_path=True)
    assert "/usr/local/bin/claude-agent-acp" in out


def test_reports_the_harness_when_it_is_absent(monkeypatch):
    """The line exists unconditionally, so an absent adapter is discoverable."""
    out = _report(
        monkeypatch,
        _state(
            MISSING,
            missing_components=("claude-agent-acp",),
            install_command="npm i -g @agentclientprotocol/claude-agent-acp",
        ),
    )
    assert "claude-acp:" in out


def test_names_the_absent_component_and_how_to_install_it(monkeypatch):
    """Claude Code needs TWO binaries, so a bare "not found" sends someone after the
    half they already have. The command rides on the next line."""
    out = _report(
        monkeypatch,
        _state(
            MISSING,
            missing_components=("claude-agent-acp",),
            install_command="npm i -g @agentclientprotocol/claude-agent-acp",
        ),
    )
    assert "claude-agent-acp not found (optional agent backend)" in out
    assert "npm i -g @agentclientprotocol/claude-agent-acp" in out


def test_never_calls_the_harness_dormant(monkeypatch):
    """The build can run it, so the old wording actively misinforms."""
    out = _report(monkeypatch, _state(INSTALLED))
    assert "dormant" not in out
    assert "installed" in out


def test_a_failed_check_is_not_reported_as_absent(monkeypatch):
    """``unknown`` means the probe could not answer, which is not evidence of absence --
    the same three-valued contract the dashboard honours."""
    out = _report(monkeypatch, _state(UNKNOWN))
    assert "not found" not in out


def test_a_raising_probe_still_prints_a_line(monkeypatch):
    """Doctor degrades to "could not check" rather than dropping the row silently."""
    out = _report(monkeypatch, None)
    assert "could not check" in out


def test_the_reporting_does_not_run_the_whole_doctor():
    """Guard the reason this file calls the helper: ``_doctor`` reaches the host.

    If someone folds the block back inline, this test's own import of the helper
    fails -- which is the signal, not a style preference.
    """
    assert callable(cli_doctor._doctor_claude_backend)


@pytest.mark.parametrize("verdict", [INSTALLED, MISSING, UNKNOWN])
def test_no_verdict_is_a_hard_failure(monkeypatch, verdict):
    """Claude Code is optional and kiro-cli is the floor, so no verdict may raise."""
    out = _report(monkeypatch, _state(verdict, missing_components=("claude-agent-acp",)))
    assert "claude-acp:" in out


@pytest.mark.parametrize("on_path", [True, False])
def test_an_install_is_never_called_selectable(monkeypatch, on_path):
    """Doctor reads the INSTALL probe, so it may not claim selectability.

    Whether the deployment may select the backend is a separate answer that
    ``apply_selectable_denials`` can say no to, and doctor never consults it. Saying
    "selectable" here would print the opposite of the truth to the operator of a
    policy-denied deployment — the same conflation of "this build can run it" with
    "this deployment may choose it" that the panel keeps apart.
    """
    out = _report(monkeypatch, _state(INSTALLED), on_path=on_path)
    assert "installed" in out
    assert "selectable" not in out
