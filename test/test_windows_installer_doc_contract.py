"""The Windows install guide may not claim CI coverage the workflow skips.

``.github/scripts/test-windows-installer.ps1`` can start the just-installed
bundled interpreter and wait for ``/api/ready``. The installer job in
``build.yml`` cannot use that: it compiles the NSIS package over a synthetic
``@echo off`` backend payload, so there is no bundled Python and no bytecode
cache to probe, and it therefore passes ``-SkipGatewayValidation``.

``docs/guides/windows-install.md`` asserted the opposite — that CI runs the
readiness probe "rather than only a synthetic import benchmark". A reader (or a
maintainer weighing a packaging change) would take that as a standing guarantee.

These tests pin the two halves against each other so the claim cannot drift
back: what the workflow actually invokes, and what the guide is allowed to say
about it. If a future change makes CI run the probe for real, the first test
fails and the guide can be restored in the same commit.
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


def test_the_installer_job_skips_gateway_validation(workflow: str) -> None:
    """The premise the guide's wording depends on, asserted rather than assumed."""
    invocations = re.findall(r"test-windows-installer\.ps1[^\n']*", workflow)
    assert invocations, "build.yml no longer invokes the Windows installer script"
    assert all(
        "-SkipGatewayValidation" in call for call in invocations
    ), f"an installer invocation now runs gateway validation: {invocations}"


def test_the_synthetic_payload_is_why_it_is_skipped(workflow: str) -> None:
    """The skip is a property of the payload, not an arbitrary flag."""
    assert "kirocrew.cmd" in workflow
    assert "@echo off" in workflow


def _readiness_sentences(guide: str) -> list[str]:
    """Every sentence of the guide that mentions the readiness probe."""
    return [s for s in re.split(r"(?<=[.])\s+", guide) if "/api/ready" in s]


def test_the_guide_discloses_that_ci_skips_the_readiness_probe(guide: str) -> None:
    """The residual this file exists for.

    Phrased as a positive requirement rather than "must not say CI": the guide
    is allowed — and now expected — to mention CI here, provided it says in the
    same breath that the installer job passes ``-SkipGatewayValidation``.
    Reinstating the old unqualified claim drops that token and reddens this.
    """
    sentences = _readiness_sentences(guide)
    assert sentences, "the guide no longer describes the readiness probe at all"
    for sentence in sentences:
        assert "SkipGatewayValidation" in sentence, (
            "the guide describes the readiness probe without disclosing that the "
            f"installer job skips it: {sentence.strip()!r}"
        )


def test_the_readiness_probe_still_exists_for_real_artifact_runs() -> None:
    """The guide's replacement wording is also load-bearing.

    It says the check runs on a real artifact rather than in CI. That is only
    honest while the script still carries the probe behind the flag.
    """
    script = _read(INSTALLER_SCRIPT)
    assert "SkipGatewayValidation" in script
    assert "/api/ready" in script


def test_the_guide_check_can_actually_fail(guide: str) -> None:
    """Self-check: a scan that matches nothing would pass as green.

    Re-runs the guide predicate against the exact sentence this change removed,
    so a future edit that breaks the split or the pattern is caught here rather
    than silently exempting the guide.
    """
    removed = (
        "CI starts the just-installed bundled interpreter against an isolated "
        "data home and requires `/api/ready` within 30 seconds, so both the "
        "packaged caches and the full gateway handoff are covered rather than "
        "only a synthetic import benchmark."
    )
    # The predicate above must reject this sentence, or it proves nothing: it
    # is picked up as a readiness sentence and carries no skip disclosure.
    assert _readiness_sentences(removed) == [removed]
    assert "SkipGatewayValidation" not in removed
    # Compare on collapsed whitespace: the guide hard-wraps and indents its
    # bullets, so a raw substring test would pass without the sentence ever
    # having been removed.
    assert removed not in re.sub(r"\s+", " ", guide)
