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
    f"publish-linux-{fmt}-{arch}" for fmt in ("appimage", "deb", "rpm") for arch in ("x64", "arm64")
)
MAC = WORKFLOWS / "sign-and-notarize.yml"
DOCKER = WORKFLOWS / "publish-docker.yml"
PROMOTION_ARTIFACT = "KiroCrew-notarized-stable-${{ needs.version.outputs.version }}"
PROMOTION_ARTIFACT_FORMAT = "format('KiroCrew-notarized-stable-{0}', needs.version.outputs.version)"
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


def _assemble_run() -> str:
    """The assembly step's shell, quote-normalized so a match ignores quoting."""
    run = _step(
        RELEASE, "github-release", "Assemble release assets (require gated macOS artifacts)"
    )["run"]
    return run.replace("'", '"')


def test_release_page_offers_every_platform_that_publishes() -> None:
    """The asset allowlist must cover every platform with a publish lane.

    ``Assemble release assets`` collects by extension, so a platform whose
    extension is missing is simply absent from the release page -- no job fails,
    nothing goes red. The Windows installer was missing from v0.2.0 through
    v0.5.0: each of those releases published one to the CDN and the update feed
    while the GitHub Release page offered none.

    Derived from the publish lanes rather than restating the list, so adding a
    lane for a new package format fails here until the release page collects it
    too.
    """
    jobs = _workflow(RELEASE)["jobs"]
    assemble = _assemble_run()

    #: The extension each publish lane's format lands on. macOS is deliberately
    #: absent: its assets come only from the gated notarized handoff, never from
    #: a glob, which is what the sibling test pins.
    lane_extension = {
        "publish-cli": (".whl", ".tar.gz"),
        "publish-linux-appimage-x64": (".AppImage",),
        "publish-linux-appimage-arm64": (".AppImage",),
        "publish-linux-deb-x64": (".deb",),
        "publish-linux-deb-arm64": (".deb",),
        "publish-linux-rpm-x64": (".rpm",),
        "publish-linux-rpm-arm64": (".rpm",),
        "publish-windows-x64": (".exe",),
    }
    for lane, extensions in lane_extension.items():
        assert lane in jobs, f"{lane} is gone; update this mapping deliberately"
        for extension in extensions:
            assert (
                f'-name "*{extension}"' in assemble
            ), f"{lane} publishes {extension} but the release page does not collect it"

    # Sidecars and feed pointers are NOT downloadable assets. The blockmap is a
    # differential-update input and latest*.yml are channel pointers published
    # by their own lanes.
    assert '-name "*.exe.blockmap"' not in assemble
    assert '-name "latest' not in assemble


def test_the_windows_installer_is_chosen_per_mode_not_by_extension() -> None:
    """A promotion run holds two installers, and only one of them is promotable.

    ``build-windows`` carries no ``if:``, so a promotion run rebuilds the
    installer even though nothing consumes it -- while the promoted candidate's
    own bytes sit in the gated bundle under ``KiroCrew-Setup.exe``.
    electron-builder's default name embeds the version, so the two names differ
    and a bare ``-name "*.exe"`` in the collection glob attaches BOTH: a stable
    page would then offer a never-soaked rebuild beside the promoted installer,
    the rebuild looking the more official of the two for carrying the version.

    So the extension glob must NOT cover ``.exe``, and the selection must branch
    on ``promote_mode`` -- the same discriminator every other lane here reads.
    """
    assemble = _assemble_run()
    glob_line = next(line for line in assemble.split("\n") if line.startswith("find artifacts"))
    assert '-name "*.exe"' not in glob_line, "an .exe glob cannot tell a rebuild from a promotion"

    step = _step(
        RELEASE, "github-release", "Assemble release assets (require gated macOS artifacts)"
    )
    assert step["env"]["PROMOTE_MODE"] == "${{ needs.version.outputs.promote_mode }}"
    assert '[ "${PROMOTE_MODE:-}" = "true" ]' in assemble
    # Promotion republishes the bundle's byte-identical installer...
    assert '"${NOTARIZED_DIR}/KiroCrew-Setup.exe"' in assemble
    # ...and a rebuild takes the producing artifact, refusing to guess between two.
    assert '-path "*build-windows-x64*" -name "*.exe"' in assemble
    assert "expected at most one Windows installer" in assemble


def test_the_release_page_waits_for_windows_without_depending_on_it() -> None:
    """The wait edge closes the race; the missing `if:` clause keeps it optional.

    ``download-artifact`` collects whatever exists when it runs, so without
    ``build-windows`` in ``needs`` a Windows build still queued on a slow runner
    is simply omitted from the page -- the same silent omission the Windows asset
    exists to end. Requiring its RESULT would be the opposite error: a Windows
    failure would take the whole release page down, when soft_fail, the optional
    bundle role and the probe-and-skip publish lane all exist to stop exactly
    that.
    """
    job = _workflow(RELEASE)["jobs"]["github-release"]
    assert "build-windows" in job["needs"]
    assert "needs.build-windows" not in job["if"]
    # Only always() lets the job run at all once a dependency may be skipped.
    assert "always()" in job["if"]


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
    cli_promote = "${{ env.HAS_PUBLISH_ROLE && env.HAS_MANIFEST_KEY && inputs.promote }}"
    cli_fresh = "${{ env.HAS_PUBLISH_ROLE && env.HAS_MANIFEST_KEY && !inputs.promote }}"
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


#: Every lane that must have published before a version is announced. This is
#: the same set ``record-promotion`` requires, so the release page and the
#: promotion record cannot disagree about what a complete publication is.
REQUIRED_PUBLICATION_LANES = (
    "publish-cli",
    *LINUX_LANES,
    "publish-docker",
    "sign-and-notarize",
)


def test_the_release_page_waits_for_every_required_publication_lane() -> None:
    """The public marker may not appear while a required lane failed.

    Each stable lane gates only on ``stable-gate``, which is a PRE-FLIGHT
    (changelog present, bytes actually shipped to insiders), so the lanes
    publish independently of one another. Gating the release page on macOS alone
    therefore lets a version become publicly visible with the CLI, a Linux
    format or the Docker tag missing, and nothing recording which lanes landed.

    Asserted over the required set rather than a hand-written list of job names,
    so a lane added to ``REQUIRED_PUBLICATION_LANES`` fails here until the page
    waits for it too.
    """
    job = _workflow(RELEASE)["jobs"]["github-release"]
    for lane in REQUIRED_PUBLICATION_LANES:
        assert lane in job["needs"], lane
        assert f"needs.{lane}.result == 'success'" in job["if"], lane


def test_the_promotion_record_and_the_release_page_require_the_same_lanes() -> None:
    """One definition of a complete publication, read from the workflow.

    If the two conditions drift, a version can carry an insider promotion record
    while the page withheld its announcement, or the reverse, and whichever is
    laxer silently becomes the real boundary.
    """
    jobs = _workflow(RELEASE)["jobs"]
    page = jobs["github-release"]["if"]
    record = jobs["record-promotion"]["if"]
    for lane in REQUIRED_PUBLICATION_LANES:
        gate = f"needs.{lane}.result == 'success'"
        assert gate in page, f"release page does not require {lane}"
        assert gate in record, f"promotion record does not require {lane}"


# ── The ALL-OR-NOTHING boundary of a stable publication ────────────────────────
#
# Waiting on every lane (above) means a lane that fails BEFORE `github-release`
# leaves no page. Two holes remained. `github-release` is itself a publishing
# lane -- it creates the page and uploads every asset in ONE action step, so a
# failure partway through left a publicly visible, half-populated release. And
# stable had no completion marker at all: `record-promotion` writes one for
# insider only, so nothing said "this version finished publishing on every lane"
# for a stable release.
#
# `record-stable-promotion` closes both. It is the only writer of the marker and
# the only thing that makes a stable page visible, so the marker and the
# announcement land together or neither lands.

#: The stable-channel completion job.
COMPLETION_JOB = "record-stable-promotion"
#: The marker file. A GitHub Release ASSET rather than a workflow artifact on
#: purpose: an artifact expires, and "did this version publish completely?"
#: outlives any retention window.
MARKER_ASSET = "stable-publication.json"
#: Steps that must not run when reconciliation finds the publication complete.
RERUN_GUARD = "steps.reconcile.outputs.already_published != 'true'"


def _completion_step(name: str) -> dict:
    return _step(RELEASE, COMPLETION_JOB, name)


def _completion_step_names() -> list[str]:
    job = _workflow(RELEASE)["jobs"][COMPLETION_JOB]
    return [str(step.get("name", "")) for step in job["steps"]]


def test_the_stable_release_page_is_created_as_a_draft() -> None:
    """A draft is how the assets can land before anyone can see them.

    ``action-gh-release`` creates the release and uploads every asset in one
    step, so published-on-create means the page is visible from its first asset
    onward: an upload that dies halfway leaves a live release offering some
    platforms and silently missing others, and the rerun then edits a page users
    have already seen. Draft-on-create makes visibility a separate single action,
    owned by the job that knows every lane finished.
    """
    step = _step(RELEASE, "github-release", "Create GitHub Release")
    assert step["with"]["draft"] == "${{ steps.page.outputs.draft }}"
    # prerelease still reads the channel directly, so nothing about which channel
    # this run is depends on the probe below.
    assert step["with"]["prerelease"] == "${{ needs.version.outputs.channel == 'insider' }}"


def test_this_workflow_never_writes_to_a_release_the_public_can_see() -> None:
    """The rule the whole draft mechanism reduces to.

    Four separate defects came out of writing to a visible release: ``draft: true``
    on an update withdrew a live page; a swallowed API error made a live page look
    absent; a rerun re-uploaded over bytes a marker already certified; and an
    interrupted re-upload left a mixed asset set visible on a public page. None is
    recoverable by anything the run can do afterwards. So a stable page is only
    ever CREATED, as a draft, and only while the public cannot see one.

    That is why ``action`` gates the upload and ``draft`` is a constant on the
    create path: there is no state left in which this action updates a visible
    release.
    """
    probe = _step(RELEASE, "github-release", "Decide whether this run may write the release page")
    assert probe["id"] == "page"
    body = probe["run"]
    # Insider short-circuits before any API call: those pages publish on create.
    assert '[ "$CHANNEL" != "stable" ]' in body
    assert "--json isDraft,assets" in body

    # Published, marked or not, is skipped. Only absent/drafted creates.
    assert 'if [ "$drafted" = "false" ]; then' in body
    writes = [line.strip() for line in body.split("\n") if "GITHUB_OUTPUT" in line]
    assert writes.count('echo "action=skip" >> "$GITHUB_OUTPUT"') == 1
    assert writes.count('echo "action=create" >> "$GITHUB_OUTPUT"') == 2
    # draft=true belongs to exactly one branch: the stable create.
    assert writes.count('echo "draft=true" >> "$GITHUB_OUTPUT"') == 1
    assert writes.count('echo "draft=false" >> "$GITHUB_OUTPUT"') == 2

    create = _step(RELEASE, "github-release", "Create GitHub Release")
    assert create["if"] == "steps.page.outputs.action == 'create'"
    assert create["with"]["draft"] == "${{ steps.page.outputs.draft }}"


def test_the_draft_probe_never_guesses_from_a_failed_api_call() -> None:
    """A transport error must not read as "no release yet".

    That guess is the withdrawal bug wearing a different hat: a rate limit or a
    5xx during a rerun of a published release would answer ``absent``, the flag
    would come back ``draft=true``, and the action's update would withdraw the
    live page. Only gh's own "release not found" means absent. Anything else
    fails the step, so the run publishes nothing -- recoverable, and strictly
    better than un-publishing something every user can see.
    """
    body = _step(RELEASE, "github-release", "Decide whether this run may write the release page")[
        "run"
    ]
    # A swallowed failure is what makes the guess possible.
    assert "2>/dev/null" not in body
    assert "|| echo absent" not in body
    assert "release not found" in body
    assert "drafted=absent" in body
    # Fails closed, loudly, on anything it cannot classify.
    assert "::error::" in body
    assert "exit 1" in body


def test_stable_publication_completes_only_after_every_required_lane() -> None:
    """The completion job is gated on the whole set, never a subset.

    Asserted over ``REQUIRED_PUBLICATION_LANES`` rather than a hand-written list,
    so a lane added to that set fails here until this job waits for it too.
    """
    job = _workflow(RELEASE)["jobs"][COMPLETION_JOB]
    condition = job["if"]

    # always() is what lets the job evaluate once a dependency may be skipped;
    # without it a skipped lane makes the job skip and nothing reports at all.
    assert "always()" in condition
    assert "needs.version.outputs.channel == 'stable'" in condition
    for lane in (*REQUIRED_PUBLICATION_LANES, "stable-gate", "github-release"):
        assert lane in job["needs"], f"{COMPLETION_JOB} does not wait for {lane}"
        assert f"needs.{lane}.result == 'success'" in condition, (
            f"{COMPLETION_JOB} does not require {lane}, so a {lane} failure would "
            "still publish the page and write the marker"
        )


def test_a_failed_lane_leaves_the_page_unpublished_and_unmarked() -> None:
    """Docker fails, or one Linux format fails: nothing announced, nothing marked.

    There is no per-failure mechanism to assert, which is the point of the shape:
    publishing the draft and writing the marker are steps of ONE job whose ``if``
    requires every lane. So the boundary is proven by showing both actions live
    in that job and nowhere else -- a second publisher would be a second
    definition of "published".
    """
    jobs = _workflow(RELEASE)["jobs"]
    condition = jobs[COMPLETION_JOB]["if"]
    for lane in ("publish-docker", "publish-linux-deb-arm64", "publish-cli"):
        assert f"needs.{lane}.result == 'success'" in condition, lane

    assert "--draft=false" in _completion_step("Publish the stable release page")["run"]
    assert MARKER_ASSET in _completion_step("Write the stable publication marker")["run"]

    # Exactly one job flips a release visible and exactly one writes the marker.
    # Scanning every other job keeps a later edit from adding a second. Reading
    # the marker is not writing it: github-release's probe asks whether it is
    # there, which is how a completed publication is left alone.
    for name, job in jobs.items():
        if name == COMPLETION_JOB:
            continue
        body = yaml.safe_dump(job)
        assert "--draft=false" not in body, f"{name} also publishes a draft release"
        assert f"> {MARKER_ASSET}" not in body, f"{name} also writes the completion marker"
        assert "gh release upload" not in body, f"{name} also attaches release assets by hand"


def test_windows_cannot_block_or_delay_a_stable_publication() -> None:
    """Windows stays optional here for the reasons it is optional everywhere else.

    ``build-windows`` soft-fails, so its ``result`` is ``success`` even when the
    build failed: a gate on it would assert nothing while coupling stable
    publication to the one lane the workflow deliberately keeps optional. It is
    absent from ``needs`` too -- the wait edge exists so an artifact DOWNLOAD
    cannot race a slow runner, and this job downloads nothing. The wait it does
    need it inherits through ``github-release``, which keeps that edge.
    """
    job = _workflow(RELEASE)["jobs"][COMPLETION_JOB]
    assert "needs.build-windows" not in job["if"]
    assert "build-windows" not in job["needs"]
    # The inherited wait must stay inherited: if the page stopped waiting for
    # Windows, this job's transitive wait would vanish with it.
    assert "build-windows" in _workflow(RELEASE)["jobs"]["github-release"]["needs"]


def test_rerunning_a_completed_stable_publication_is_a_no_op() -> None:
    """Reconciliation for an interrupted rerun: already published means done.

    A rerun of a run whose publication finished must not fail (that reports a
    problem nobody has) and must not re-edit a live page. It reads the release's
    own ``isDraft`` -- the state of the world, not a record of what this run did,
    which is what makes it hold for a rerun days later on a different runner.
    """
    reconcile = _completion_step("Reconcile an interrupted or repeated run")
    assert reconcile["id"] == "reconcile"
    body = reconcile["run"]
    assert "--json isDraft,assets" in body
    assert "already_published" in body and "GITHUB_OUTPUT" in body
    # A completed rerun is a NOTICE, not a failure: nothing is wrong with it.
    complete = body.index('[ "$drafted" = "false" ] && [ "$marked" = "true" ]')
    anomaly = body.index('elif [ "$drafted" = "false" ]; then')
    assert complete < body.index("::notice::") < anomaly
    # The only failure here is the anomaly branch, never the rerun branch.
    assert body.index("::error::") > anomaly

    for name in (
        "Write the stable publication marker",
        "Attach the completion marker to the release",
        "Publish the stable release page",
    ):
        assert (
            _completion_step(name)["if"] == RERUN_GUARD
        ), f"{name!r} runs even when the publication already completed"


def test_the_marker_is_attached_before_the_page_becomes_visible() -> None:
    """Order is the atomicity: the page goes public already carrying its marker.

    Publishing first would leave a visible release with no completion record for
    as long as the upload takes, and a failure in between would leave that state
    permanently -- the "visible but unmarked" condition this job exists to remove.
    """
    names = _completion_step_names()
    assert names.index("Write the stable publication marker") < names.index(
        "Attach the completion marker to the release"
    )
    assert names.index("Attach the completion marker to the release") < names.index(
        "Publish the stable release page"
    )
    # Publishing is the LAST thing the job does: a step after it would be work
    # happening while the release is already public.
    assert names[-1] == "Publish the stable release page"


def test_the_marker_records_what_it_certifies() -> None:
    """A marker that only says "done" cannot be audited against the lanes.

    So it carries the identity of the publication -- tag, version, source commit,
    the run that did it -- and the lane set that had to succeed for it to exist.
    """
    marker = _completion_step("Write the stable publication marker")
    env = marker["env"]
    assert env["TAG"] == "${{ github.ref_name }}"
    assert env["VERSION"] == "${{ needs.version.outputs.version }}"
    assert env["BASE_VERSION"] == "${{ needs.version.outputs.base_version }}"
    assert env["PROMOTE_MODE"] == "${{ needs.version.outputs.promote_mode }}"
    assert env["DOCKER_DIGEST"] == "${{ needs.publish-docker.outputs.digest }}"
    for lane in REQUIRED_PUBLICATION_LANES:
        assert lane in env["REQUIRED_LANES"], f"the marker does not name {lane}"

    body = marker["run"]
    for field in ("source_sha", "source_run_id", "required_lanes", "completed_at"):
        assert field in body, field


def test_the_completion_marker_requires_the_same_lanes_as_the_release_page() -> None:
    """One definition of a complete publication, shared by all three gates.

    If they drift, whichever is laxest silently becomes the real boundary: a page
    announced with a lane only the marker required, or a version marked complete
    with a lane only the page waited for.
    """
    jobs = _workflow(RELEASE)["jobs"]
    page = jobs["github-release"]["if"]
    completion = jobs[COMPLETION_JOB]["if"]
    record = jobs["record-promotion"]["if"]
    for lane in REQUIRED_PUBLICATION_LANES:
        gate = f"needs.{lane}.result == 'success'"
        assert gate in page, f"the release page does not require {lane}"
        assert gate in completion, f"the completion marker does not require {lane}"
        assert gate in record, f"the insider promotion record does not require {lane}"


def test_the_completion_job_holds_only_the_write_it_needs() -> None:
    """It edits a release, so ``contents: write`` -- and nothing beyond it.

    No AWS identity and no package scope: the same least-privilege split that
    keeps a compromise of one job from being able to both sign and publish.
    """
    job = _workflow(RELEASE)["jobs"][COMPLETION_JOB]
    assert job["permissions"] == {"contents": "write"}
    assert job["timeout-minutes"] == 10


def test_completion_is_the_marker_not_the_visibility() -> None:
    """A visible page with no marker is INCOMPLETE, and reconciliation says so.

    A draft carries a "Publish release" button, so an operator can make a
    half-populated page public by hand -- and releases published before this job
    existed are public with nothing certifying them either. Reading visibility
    alone would call both states complete and leave them uncertified forever, so
    the no-op branch requires the marker to be ON the release as well. That is
    also what gives the marker an in-workflow reader: it is the state this job
    reconciles against, not a file nothing consults.
    """
    body = _completion_step("Reconcile an interrupted or repeated run")["run"]
    assert MARKER_ASSET in body, "reconciliation does not look for the marker at all"
    # Both conditions, and "complete" only when both hold.
    assert 'if [ "$drafted" = "false" ] && [ "$marked" = "true" ]; then' in body
    assert "already_published=true" in body
    assert "already_published=false" in body
    # Visible-but-unmarked is neither completed nor silently repaired: marking it
    # would certify an asset set this run never uploaded, and repairing it would
    # rewrite a page users can already see. It fails and names both options.
    assert "exit 1" in body
    assert "delete the release page" in body


def test_a_published_release_is_never_uploaded_to_again() -> None:
    """Both published states skip the upload, for two different reasons.

    Published AND marked: the marker certifies the set of bytes that page carries,
    so re-uploading would replace certified bytes, and an upload that died partway
    would leave a certified page carrying a mixed set while reconciliation, seeing
    a marker, called it complete.

    Published and UNMARKED: the page is live, so an interrupted re-upload is a
    mixed asset set users can see -- on a release nothing certifies either way.
    The probe says so in a warning and reconciliation refuses it outright.
    """
    probe = _step(RELEASE, "github-release", "Decide whether this run may write the release page")
    body = probe["run"]
    # One test for both: the skip branch is entered on `drafted == false` alone,
    # before the marker is consulted at all.
    skip = body.index('if [ "$drafted" = "false" ]; then')
    assert body.index('echo "action=skip" >> "$GITHUB_OUTPUT"') > skip
    assert body.index('if [ "$marked" = "true" ]; then') > skip
    assert "::warning::" in body, "an unmarked public release must be reported"

    # The marker predicate is shared with reconciliation so the two cannot drift
    # into two notions of "completed".
    predicate = '[ "$drafted" = "false" ] && [ "$marked" = "true" ]'
    assert predicate in _completion_step("Reconcile an interrupted or repeated run")["run"]
