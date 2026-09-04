"""Structural contracts for stable releases, in both of their modes.

The executable manifest tests prove digest and archive handling. These tests
pin the GitHub Actions wiring for the two ways a bare stable tag can ship:

* REBUILD (the default) builds fresh from the cleared commit on the stable
  channel, so the shipped bytes carry a bare ``X.Y.Z``.
* PROMOTE (opt in per version via the ``STABLE_PROMOTE_BYTES`` repo variable)
  republishes the soaked candidate's exact bytes, so stable runs the identical
  binary insiders validated -- at the cost of shipping that candidate's
  ``-insider.N`` / ``rcN`` stamp, which nothing downstream can re-stamp.

Rebuild is the default because the version a stable user sees must not carry a
prerelease suffix, and promotion can never deliver that. The modes are mutually
exclusive and every downstream "promotion or fresh?" choice reads
``promote_mode``, never ``channel``, so neither mode can half-apply and leave
one lane disagreeing with the rest of the release.
"""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
CLI = WORKFLOWS / "publish-cli.yml"
LINUX = WORKFLOWS / "publish-linux.yml"
#: Every (format, arch) Linux publish lane release.yml calls. Each is a
#: separate job because publish-linux.yml writes one immutable versioned key
#: per invocation.
LINUX_LANES = tuple(
    f"publish-linux-{fmt}-{arch}"
    for fmt in ("appimage", "deb", "rpm")
    for arch in ("x64", "arm64")
)
MAC = WORKFLOWS / "sign-and-notarize.yml"
DOCKER = WORKFLOWS / "publish-docker.yml"
PROMOTION_ARTIFACT = "KiroCrew-notarized-stable-${{ needs.version.outputs.version }}"
PROMOTION_ARTIFACT_FORMAT = (
    "format('KiroCrew-notarized-stable-{0}', needs.version.outputs.version)"
)
#: Every lane's ``promote`` input reads promote_mode, never channel, so an
#: opt-in stable rebuild flips all of them together or none of them.
PROMOTE_EXPRESSION = "${{ needs.version.outputs.promote_mode == 'true' }}"


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _inputs(path: Path) -> dict:
    return _workflow(path)[True]["workflow_call"]["inputs"]


def _step(path: Path, job: str, name: str) -> dict:
    steps = _workflow(path)["jobs"][job]["steps"]
    return next(step for step in steps if step.get("name") == name)


def test_release_base_requires_exact_three_component_numeric_version() -> None:
    derive = _step(RELEASE, "version", "Derive version + channel from tag")["run"]
    assert '[[ "$BASE" =~ ^[0-9]+\\.[0-9]+\\.[0-9]+$ ]]' in derive
    assert 'case "$BASE" in' not in derive


def test_stable_tag_resolves_candidate_and_never_enters_build_jobs() -> None:
    jobs = _workflow(RELEASE)["jobs"]

    # promote_mode, not channel: a stable REBUILD also runs on the stable
    # channel, and it must take the fresh/insider-shaped branch instead.
    assert jobs["resolve-promotion"]["if"] == "needs.version.outputs.promote_mode == 'true'"
    assert jobs["resolve-promotion"]["permissions"] == {
        "actions": "read",
        "contents": "read",
    }
    # The build lanes stay off the PROMOTION path. They gain exactly one extra
    # entrance -- an opt-in rebuild -- and nothing else may widen them.
    build_gate = (
        "needs.version.outputs.channel == 'insider' || needs.version.outputs.rebuild == 'true'"
    )
    assert jobs["build-wheel"]["if"] == build_gate
    assert jobs["build-desktop"]["if"] == build_gate

    resolve = _step(RELEASE, "resolve-promotion", "Resolve and verify immutable candidate bundle")[
        "run"
    ]
    assert "scripts/release_promotion.py resolve" in resolve
    assert '--source-sha "${GITHUB_SHA}"' in resolve
    assert "--base-version" in resolve
    assert "--archive-path" in resolve

    handoff = _step(RELEASE, "resolve-promotion", "Attach verified bundle to this run")
    assert handoff["with"]["name"] == PROMOTION_ARTIFACT
    assert handoff["with"]["if-no-files-found"] == "error"


def test_stable_rebuild_is_the_default_and_promotion_is_opt_in_per_version() -> None:
    """Stable must not be able to ship an RC-stamped version by omission.

    Promotion republishes the candidate's own bytes, which carry its ``rcN``
    stamp in the wheel name, the feed, ``pip show`` and ``kirocrew --version``.
    That mode still exists -- it is the only one where stable runs the exact
    binary insiders validated -- but it cannot be the default, because then
    forgetting a setting silently ships a prerelease-looking version to stable
    users. So rebuild is what a bare stable tag does with no configuration, and
    byte reuse has to NAME the base version being released (mirroring
    ``STABLE_GATE_OVERRIDE``) so it cannot be left switched on.
    """
    jobs = _workflow(RELEASE)["jobs"]
    outputs = jobs["version"]["outputs"]
    assert outputs["rebuild"] == "${{ steps.channel.outputs.rebuild }}"
    assert outputs["promote_mode"] == "${{ steps.channel.outputs.promote_mode }}"

    derive = _step(RELEASE, "version", "Derive version + channel from tag")
    assert derive["env"]["STABLE_PROMOTE_BYTES"] == "${{ vars.STABLE_PROMOTE_BYTES }}"
    run = derive["run"]

    # Byte reuse is scoped to one exact base version, and only on stable.
    assert '[ "$CHANNEL" = "stable" ] && [ "${STABLE_PROMOTE_BYTES:-}" = "$BASE" ]' in run
    # Rebuild is the fallthrough: stable-and-not-promoting. Mutually exclusive,
    # and no stable tag can end up taking neither path.
    assert '[ "$CHANNEL" = "stable" ] && [ "$PROMOTE_MODE" != "true" ]' in run
    assert 'echo "rebuild=$REBUILD" >> "$GITHUB_OUTPUT"' in run
    assert 'echo "promote_mode=$PROMOTE_MODE" >> "$GITHUB_OUTPUT"' in run
    # The old spelling must not linger anywhere: a leftover STABLE_REBUILD would
    # read as the switch that turns rebuild ON, when rebuild is now the default.
    assert "STABLE_REBUILD" not in run

    # A rebuild publishes freshly built artifacts, so it must NOT reach for the
    # promotion handoff on any lane.
    for name in ("publish-cli", "publish-docker"):
        assert jobs[name]["with"]["wheel_artifact"].startswith(
            "${{ needs.version.outputs.promote_mode == 'true' &&"
        ), name
    for lane in LINUX_LANES:
        assert jobs[lane]["with"]["build_artifact"].startswith(
            "${{ needs.version.outputs.promote_mode == 'true' &&"
        ), lane


def test_stable_gate_requires_the_three_version_files_to_declare_the_bare_version() -> None:
    """The branch must agree with the tag, not just the artifact.

    A rebuild re-stamps the version at build time, so the ARTIFACT is right even
    when the release branch still declares the RC spelling. That is exactly how
    the 0.4.0 promotion was nearly tagged on a commit still declaring
    ``0.4.0-rc.9``: only the tag name was checked. This gate compares the tag to
    all three declarations so a missing drop-RC-suffix PR fails the release
    instead of leaving source installs on a stale version.
    """
    gate = _step(RELEASE, "stable-gate", "Verify stable publication preconditions")
    assert gate["env"]["PROMOTE_MODE"] == "${{ needs.version.outputs.promote_mode }}"
    run = gate["run"]
    for path in (
        "src/kiro_crew/__init__.py",
        "pyproject.toml",
        "website/electron/package.json",
    ):
        assert path in run, path
    # Compared against the tag's own version, and a mismatch is a failure rather
    # than a warning.
    assert 'if [ "$declared" = "$VERSION" ]; then' in run
    assert "Land the drop-RC-suffix PR on the release branch before tagging." in run
    # Promotion is allowed but must announce the cost in the run log.
    assert "will ship an RC-stamped version" in run


def test_every_stable_lane_consumes_the_verified_handoff() -> None:
    jobs = _workflow(RELEASE)["jobs"]
    for name in ("publish-cli", *LINUX_LANES, "publish-docker", "sign-and-notarize"):
        job = jobs[name]
        assert "resolve-promotion" in job["needs"]
        assert "needs.resolve-promotion.result == 'success'" in job["if"]

    assert PROMOTION_ARTIFACT_FORMAT in jobs["publish-cli"]["with"]["wheel_artifact"]
    assert jobs["publish-cli"]["with"]["promote"] == PROMOTE_EXPRESSION

    for lane in LINUX_LANES:
        assert PROMOTION_ARTIFACT_FORMAT in jobs[lane]["with"]["build_artifact"], lane
        assert "resolve-promotion.outputs.source_version" in jobs[lane]["with"]["version"], lane
        assert jobs[lane]["with"]["promote"] == PROMOTE_EXPRESSION, lane

    docker_inputs = jobs["publish-docker"]["with"]
    assert docker_inputs["promote"] == PROMOTE_EXPRESSION
    assert "resolve-promotion.outputs.docker_digest" in docker_inputs["promote_digest"]

    mac_inputs = jobs["sign-and-notarize"]["with"]
    assert PROMOTION_ARTIFACT_FORMAT in mac_inputs["promotion_artifact"]
    assert "resolve-promotion.outputs.source_version" in mac_inputs["version"]
    assert mac_inputs["promote"] == PROMOTE_EXPRESSION


def test_github_release_selects_explicit_versioned_macos_handoff() -> None:
    verify = _step(RELEASE, "github-release", "Verify promoted release bytes")["run"]
    assert f'--bundle-dir "artifacts/{PROMOTION_ARTIFACT}"' in verify

    assemble = _step(
        RELEASE, "github-release", "Assemble release assets (require gated macOS artifacts)"
    )["run"]
    assert (
        'NOTARIZED_DIR="artifacts/KiroCrew-notarized-${{ needs.version.outputs.channel }}-'
        '${{ needs.version.outputs.version }}"' in assemble
    )
    assert "KiroCrew-notarized-stable-promotion" not in assemble
    assert "*KiroCrew-notarized-*" not in assemble


def test_prerelease_candidate_runs_same_sha_test_gate() -> None:
    jobs = _workflow(RELEASE)["jobs"]
    gate = jobs["release-candidate-tests"]
    assert gate["if"] == "needs.version.outputs.channel == 'insider'"
    assert gate["needs"] == "version"

    checkout = next(
        step for step in gate["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ github.sha }}"

    verify = _step(RELEASE, "release-candidate-tests", "Verify exact candidate SHA")
    assert "git rev-parse HEAD" in verify["run"]
    assert "GITHUB_SHA" in verify["run"]

    tests = _step(RELEASE, "release-candidate-tests", "Run release candidate tests")
    assert "pytest" in tests["run"]
    assert "--no-cov" in tests["run"]


def test_prerelease_record_waits_for_test_gate_and_all_publish_lanes() -> None:
    job = _workflow(RELEASE)["jobs"]["record-promotion"]
    assert set(job["needs"]) == {
        "version",
        "release-candidate-tests",
        "publish-cli",
        "publish-linux-appimage-x64",
        "publish-linux-appimage-arm64",
        "publish-linux-deb-x64",
        "publish-linux-deb-arm64",
        "publish-linux-rpm-x64",
        "publish-linux-rpm-arm64",
        "publish-docker",
        "sign-and-notarize",
        "build-windows",
    }
    for dependency in (
        "release-candidate-tests",
        "publish-cli",
        "publish-linux-appimage-x64",
        "publish-linux-appimage-arm64",
        "publish-linux-deb-x64",
        "publish-linux-deb-arm64",
        "publish-linux-rpm-x64",
        "publish-linux-rpm-arm64",
        "publish-docker",
        "sign-and-notarize",
    ):
        assert f"needs.{dependency}.result == 'success'" in job["if"]

    # build-windows is WAITED ON but never REQUIRED, and the difference is the
    # whole design. Waiting is mandatory: the Windows role is optional, so
    # assembling before the installer artifact exists would silently record a
    # Windows-less candidate from a build that actually succeeded. Requiring
    # success is forbidden: it would make stable promotion depend on the Windows
    # build, the coupling soft_fail exists to prevent -- and soft_fail forces that
    # result to 'success' anyway, so the check would assert nothing at all.
    assert "needs.build-windows.result" not in job["if"]

    assemble = _step(RELEASE, "record-promotion", "Assemble canonical promotion bundle")
    assert assemble["env"]["DOCKER_DIGEST"] == "${{ needs.publish-docker.outputs.digest }}"
    run = assemble["run"]
    assert "scripts/release_promotion.py create" in run
    assert '--source-sha "${GITHUB_SHA}"' in run
    assert '--source-run-id "${GITHUB_RUN_ID}"' in run
    assert '--docker-digest "${DOCKER_DIGEST}"' in run

    upload = _step(RELEASE, "record-promotion", "Upload immutable promotion record")
    assert "stable-promotion-" in upload["with"]["name"]
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] == 90


def test_file_publishers_verify_manifest_and_prior_provenance() -> None:
    for path in (CLI, LINUX):
        inputs = _inputs(path)
        assert inputs["promote"]["default"] is False
        assert inputs["promotion_base_version"]["default"] == ""

    cli_manifest = _step(CLI, "publish-cli", "Verify immutable promotion bundle")
    cli_attest = _step(CLI, "publish-cli", "Attest wheel provenance")
    cli_verify = _step(CLI, "publish-cli", "Verify promoted wheel provenance")
    cli_promote = (
        "${{ env.HAS_PUBLISH_ROLE && env.HAS_MANIFEST_KEY && inputs.promote }}"
    )
    cli_fresh = (
        "${{ env.HAS_PUBLISH_ROLE && env.HAS_MANIFEST_KEY && !inputs.promote }}"
    )
    assert cli_manifest["if"] == cli_promote
    assert cli_attest["if"] == cli_fresh
    assert cli_verify["if"] == cli_promote
    assert "gh attestation verify" in cli_verify["run"]

    linux_manifest = _step(LINUX, "publish-linux", "Verify immutable promotion bundle")
    linux_attest = _step(LINUX, "publish-linux", "Attest artifact provenance")
    linux_verify = _step(LINUX, "publish-linux", "Verify promoted artifact provenance")
    linux_promote = "env.HAS_SIGNING_SECRETS && inputs.promote"
    linux_fresh = "env.HAS_SIGNING_SECRETS && !inputs.promote"
    assert linux_manifest["if"] == linux_promote
    assert linux_attest["if"] == linux_fresh
    assert linux_verify["if"] == linux_promote
    assert "gh attestation verify" in linux_verify["run"]


def test_macos_promotion_skips_transformations_and_verifies_final_bytes() -> None:
    jobs = _workflow(MAC)["jobs"]
    assert jobs["sign"]["if"] == "${{ !inputs.promote }}"
    publish_if = jobs["publish"]["if"]
    assert "always()" in publish_if
    assert "needs.notarize.result == 'success'" in publish_if
    assert "needs.notarize.result == 'skipped'" in publish_if

    final_attest = _step(MAC, "notarize", "Attest final shipping artifacts")
    subjects = final_attest["with"]["subject-path"]
    assert "work/notarized.zip" in subjects
    assert "work/*.dmg" in subjects

    manifest = _step(MAC, "publish", "Verify immutable promotion bundle")
    provenance = _step(MAC, "publish", "Verify promoted macOS provenance")
    assert "inputs.promote" in manifest["if"]
    assert provenance["run"].count("gh attestation verify") == 2


def test_docker_promotion_input_defaults_to_no_promotion() -> None:
    inputs = _inputs(DOCKER)
    assert inputs["promote"]["default"] is False
    assert inputs["promote_digest"]["default"] == ""
    build = _step(DOCKER, "publish-docker", "Build and push (version tag)")
    attest = _step(DOCKER, "publish-docker", "Attest image provenance (fresh build)")
    promote = _step(DOCKER, "publish-docker", "Record promoted immutable version tag")
    assert "!inputs.promote" in build["if"]
    assert "!inputs.promote" in attest["if"]
    assert "inputs.promote" in promote["if"]
    assert '"${IMAGE}@${DIGEST}"' in promote["run"]
