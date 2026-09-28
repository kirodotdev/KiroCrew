"""A failed arm64 build must not skip the x64 Linux publish, asserted from source.

WHY THIS FILE EXISTS. ``nightly.yml`` and ``release.yml`` each invoke
``publish-linux.yml`` six times -- appimage, deb and rpm, x64 and arm64 -- and
every one of them waits on ``build-desktop``, the reusable-workflow CALLER job.
That caller aggregates a three-leg matrix (``macos-15``, ``ubuntu-22.04``,
``ubuntu-22.04-arm``) plus the package smoke, so its result cannot say WHICH leg
failed. Before this, a failed arm64 build therefore skipped all six publishers,
three of which are x64 and had a complete artifact sitting in the run.

``nightly.yml`` now passes ``soft_fail_arm64: true``, which marks ONLY the arm64
build leg ``continue-on-error``. That is the same shape ``build-windows.yml``
already uses for the whole Windows lane, and it is safe on the nightly lane for
a reason that lives outside the workflow: every key, feed file and alias
``publish-linux.yml`` writes is arch-resolved (pinned by
``test_publish_feed_contract.py``), so an x64-only publish writes nothing an
arm64 client reads, and the run records no promotion candidate.

Four properties have to hold for that to be per-arch independence rather than a
loosened gate, and every one of them is quiet when it breaks:

1. Exactly one leg may be relaxed, and it must be the arm64 one. A second
   eligible leg turns a scoped switch into a blanket one.
2. ``smoke-linux-packages`` must never carry the relaxation. It is the only
   failure in that workflow that is a statement about the bytes being
   published, and a deb or rpm that will not install has to hold both arches.
   The same reasoning is written out at ``publish-windows.yml``'s caller.
3. The nightly publishers must keep ``build-desktop`` in ``needs`` and stay
   free of an ``if:``. ``needs`` is what makes them wait for the build and the
   smoke; an ``if:`` reaching past a failed caller would also reach past a
   rejected package.
4. ``release.yml`` must NOT pass the input. Its run records the stable
   promotion candidate and ``scripts/release_promotion.py`` lists all three
   arm64 Linux roles in ``REQUIRED_ARTIFACT_NAMES``, so publishing x64 alone
   there burns immutable keys for a version that can never be promoted.
   Moving those roles to optional changes what a complete release IS, which is
   a release-policy decision; the workflow's own comment names where it is
   tracked.

The boolean-safety of the expression, and the input being declared on both
triggers, are pinned by ``test_nightly_version_contract.py``; a missing trigger
declaration makes the key non-boolean and GitHub rejects the workflow at startup
with zero jobs and no log.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"

SOFT_FAIL_INPUT = "soft_fail_arm64"
ELIGIBLE_KEY = "soft_fail_eligible"
ARM64_ARTIFACT = "build-linux-arm64"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _legs() -> list[dict]:
    return _load("build-desktop.yml")["jobs"]["build-desktop"]["strategy"]["matrix"]["include"]


def _linux_publishers(workflow: str) -> dict[str, dict]:
    jobs = _load(workflow)["jobs"]
    return {
        name: job
        for name, job in jobs.items()
        if str(job.get("uses", "")).endswith("publish-linux.yml")
    }


def test_exactly_one_build_leg_may_soft_fail_and_it_is_arm64() -> None:
    legs = _legs()
    # Every leg states the key, so "absent" can never masquerade as "false" and
    # hide which leg the switch was meant for.
    for leg in legs:
        assert ELIGIBLE_KEY in leg, f"leg {leg['os']} does not state {ELIGIBLE_KEY}"
    eligible = [leg for leg in legs if leg[ELIGIBLE_KEY] is True]
    assert len(eligible) == 1, (
        "exactly one build leg may be soft-fail eligible; a second one turns a "
        "scoped switch into a blanket one that also stops a macOS or x64 build "
        "failure from holding the jobs that legitimately depend on it"
    )
    assert eligible[0]["artifact-name"] == ARM64_ARTIFACT
    assert eligible[0]["os"] == "ubuntu-22.04-arm"


def test_the_relaxation_is_gated_on_both_the_input_and_the_leg() -> None:
    expr = str(_load("build-desktop.yml")["jobs"]["build-desktop"]["continue-on-error"])
    # Both halves: without the input half every caller gets the relaxation,
    # without the leg half every leg does.
    assert f"inputs.{SOFT_FAIL_INPUT} == true" in expr, expr
    assert "matrix.soft_fail_eligible == true" in expr, expr


def test_the_package_smoke_never_carries_the_relaxation() -> None:
    smoke = _load("build-desktop.yml")["jobs"]["smoke-linux-packages"]
    assert "continue-on-error" not in smoke, (
        "smoke-linux-packages failing means a deb or rpm will not install in its "
        "target distro, which must hold the publish for BOTH arches -- it is the "
        "one failure in this workflow that is about the bytes being published"
    )
    # It also has to keep depending on the build, or it would run against an
    # artifact from nowhere.
    assert smoke["needs"] == "build-desktop"


def test_nightly_relaxes_the_arm64_build_and_the_release_lane_does_not() -> None:
    assert _load("nightly.yml")["jobs"]["build-desktop"]["with"][SOFT_FAIL_INPUT] is True
    release_with = _load("release.yml")["jobs"]["build-desktop"]["with"]
    assert SOFT_FAIL_INPUT not in release_with, (
        "a release run records the stable promotion candidate, where all three "
        "arm64 Linux roles are REQUIRED (scripts/release_promotion.py), so "
        "publishing x64 alone would burn immutable keys for an unpromotable "
        "version -- that trade is #1030's to settle"
    )


def test_every_nightly_linux_publisher_still_waits_on_the_build_and_the_gates() -> None:
    publishers = _linux_publishers("nightly.yml")
    assert len(publishers) == 6, sorted(publishers)
    for name, job in publishers.items():
        assert "if" not in job, (
            f"{name} grew an `if:`. Reaching past a failed caller result also "
            "reaches past a rejected package: the smoke's verdict arrives "
            "through exactly this dependency."
        )
        assert set(job["needs"]) >= {
            "build-desktop",
            "dependency-vulnerability-gate",
            "platform-tests",
        }, f"{name} dropped a dependency it must still wait on: {job['needs']}"


def test_the_release_lane_still_requires_the_aggregate_build_result() -> None:
    for name, job in _linux_publishers("release.yml").items():
        assert "needs.build-desktop.result == 'success'" in str(job["if"]), (
            f"{name} no longer requires the aggregate build result. That is the "
            "coupling #1030 has to decide before it is loosened, because this "
            "lane's run is what records the promotion candidate."
        )


def test_each_publisher_consumes_the_artifact_its_own_leg_uploads() -> None:
    """The eligible leg and the lane that goes red for it must stay bound.

    If a rename split these apart, the arm64 publishers would keep publishing
    from some other artifact while the relaxed leg quietly stopped mattering.
    """
    uploaded = {leg["artifact-name"] for leg in _legs()}
    for name, job in _linux_publishers("nightly.yml").items():
        arch = job["with"]["arch"]
        artifact = job["with"]["build_artifact"]
        assert artifact in uploaded, f"{name} consumes {artifact}, which no build leg uploads"
        assert artifact == f"build-linux-{arch}", f"{name} publishes {arch} from {artifact}"
