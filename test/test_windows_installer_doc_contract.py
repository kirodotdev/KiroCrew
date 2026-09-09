"""The Windows install guide's CI claim and `build.yml` must describe one reality.

``build.yml``'s installer job bundles a real python-build-standalone runtime
carrying the wheel ``build-wheel`` produced, and runs
``.github/scripts/test-windows-installer.ps1`` with NO skip flag, so the
installed gateway's readiness probe is a per-PR gate. Documentation that claims
LESS coverage than CI has is still documentation a maintainer weighs a packaging
change against, so the guide may not describe that probe as skipped.

The two halves are pinned against each other:

* what the workflow actually invokes, and that its payload can support it;
* what the guide is allowed to say, which must not describe the
  probe as skipped.

If a change reintroduces a stub payload, the payload test fails and names the
reason rather than leaving the guide quietly wrong.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"
INSTALLER_SCRIPT = ROOT / ".github" / "scripts" / "test-windows-installer.ps1"
INSTALL_GUIDE = ROOT / "docs" / "guides" / "windows-install.md"

# Every read is explicit UTF-8: these files carry em dashes and box-drawing
# characters, and a non-UTF-8 default codepage would fail the decode instead of
# the assertion.
_ENCODING = "utf-8"


def _read(path: Path) -> str:
    return path.read_text(encoding=_ENCODING)


@pytest.fixture(scope="module")
def workflow() -> str:
    return _read(BUILD_WORKFLOW)


@pytest.fixture(scope="module")
def guide() -> str:
    return _read(INSTALL_GUIDE)


def _invocations(workflow: str) -> list[str]:
    """Every `run:` line that invokes the installer script."""
    return re.findall(r"test-windows-installer\.ps1[^\n']*", workflow)


def test_the_installer_job_runs_gateway_validation(workflow: str) -> None:
    """The premise the guide's wording now depends on, asserted not assumed.

    A comment mentioning the flag is fine and expected; passing it is not. The
    match is therefore against the INVOCATION text only, not the whole file.
    """
    invocations = _invocations(workflow)
    assert invocations, "build.yml no longer invokes the Windows installer script"
    skipped = [call for call in invocations if "-SkipGatewayValidation" in call]
    assert not skipped, (
        "an installer invocation skips gateway validation again; the guide claims "
        f"it runs per PR: {skipped}"
    )


def test_the_payload_can_actually_boot_a_gateway(workflow: str) -> None:
    """Dropping the flag is only honest while the payload has an interpreter.

    The stub had no ``python.exe``, so the skip was a property of the payload
    rather than an arbitrary flag. Pin the three things that replaced it: the
    wheel is consumed, a real interpreter is provisioned, and the measured
    import closure is precompiled so the script's pyc floor has something to
    find. Without this test, deleting the assembly step would leave a green
    invocation that fails only on a Windows runner.
    """
    assert "needs: build-wheel" in workflow, "the job does not consume the built wheel"
    assert "cpython-3.12-windows-x86_64-none" in workflow, "no PBS interpreter is provisioned"
    assert "precompile_windows.py" in workflow, "the gateway import closure is not precompiled"


def test_the_fake_backend_keeps_readiness_offline() -> None:
    """Readiness must not depend on a real model, a network or a sign-in.

    Asserted on the SCRIPT, which owns the gateway leg's environment: the ACP
    backend is pointed at the fake one shipping inside the payload under test.
    A future edit that drops this makes the 30-second ceiling depend on a model
    download and turns a real gate into a flake.
    """
    script = _read(INSTALLER_SCRIPT)
    assert "KIROCREW_KIRO_BIN" in script
    assert "kiro_crew.testing.fake_acp_backend" in script
    assert "KIROCREW_SKIP_MODEL_DOWNLOAD" in script


def test_the_readiness_probe_still_exists(workflow: str) -> None:
    """The guide's wording is load-bearing on the probe existing at all."""
    script = _read(INSTALLER_SCRIPT)
    assert "/api/ready" in script
    # The floor is passed explicitly because this job omits the voice extras the
    # script's 1000 default describes. A caller that stops passing it would
    # redden on a Windows runner for a reason unrelated to the artifact.
    assert "MinStartupPycs" in script
    assert "-MinStartupPycs" in " ".join(_invocations(workflow))


def _readiness_sentences(guide: str) -> list[str]:
    """Every sentence of the guide that mentions the readiness probe."""
    return [s for s in re.split(r"(?<=[.])\s+", guide) if "/api/ready" in s]


def test_the_guide_does_not_describe_the_probe_as_skipped(guide: str) -> None:
    """The residual this file exists for, inverted.

    The guide must describe the readiness probe, and no sentence describing it
    may claim the installer job skips it. Leaving the old disclosure in place
    after the skip was removed understates the coverage a reader is relying on.
    """
    sentences = _readiness_sentences(guide)
    assert sentences, "the guide no longer describes the readiness probe at all"
    stale = [s for s in sentences if "SkipGatewayValidation" in s and "no longer" not in s]
    assert not stale, (
        "the guide still presents the readiness probe as skipped in CI, but the "
        f"installer job runs it: {[s.strip() for s in stale]}"
    )


def test_the_guide_check_can_actually_fail(guide: str) -> None:
    """Self-check: a scan that matches nothing would pass as green.

    Re-runs the guide predicate against the exact disclosure this change
    removed, so a future edit that breaks the sentence split or the pattern is
    caught here rather than silently exempting the guide.
    """
    removed = (
        "What `build.yml` enforces on every push is the native installer's "
        "performance ceiling and its install-location contract, and it runs the "
        "script with `-SkipGatewayValidation`."
    )
    # The predicate must REJECT this sentence, or it proves nothing: it is picked
    # up as a readiness sentence only when it mentions the probe, so assert both
    # halves of the shape it is meant to catch.
    disclosure = "It requires `/api/ready` but the job passes `-SkipGatewayValidation`."
    assert _readiness_sentences(disclosure) == [disclosure]
    assert "SkipGatewayValidation" in disclosure
    # Compare on collapsed whitespace: the guide hard-wraps and indents its
    # bullets, so a raw substring test would pass without the sentence ever
    # having been removed.
    assert removed not in re.sub(r"\s+", " ", guide)


def test_the_windows_smoke_install_is_gated_on_a_real_artifact() -> None:
    """``needs`` alone cannot see a soft-failed build.

    ``build-windows`` runs under ``continue-on-error`` on publish runs, and a job
    that failed under it still reports success to its dependents, so the smoke
    install would download an artifact that was never uploaded and redden exactly
    the release run ``soft_fail`` keeps green. The gate is an output the build
    sets only after the upload step succeeded.
    """
    import yaml

    text = (ROOT / ".github" / "workflows" / "build-windows.yml").read_text(encoding=_ENCODING)
    jobs = yaml.safe_load(text)["jobs"]
    build, smoke = jobs["build-windows"], jobs["smoke-install-windows"]
    assert build["outputs"]["artifact_uploaded"] == "${{ steps.uploaded.outputs.uploaded }}"
    step_ids = [s.get("id") for s in build["steps"]]
    upload_idx = next(
        i for i, s in enumerate(build["steps"]) if s.get("name") == "Upload desktop artifact"
    )
    assert step_ids.index("uploaded") > upload_idx, "the marker step must follow the upload"
    assert smoke["needs"] == "build-windows"
    assert smoke["if"] == "needs.build-windows.outputs.artifact_uploaded == 'true'"
