"""Contract tests for Windows Authenticode signing via AWS Signer.

Windows signing is unlike the macOS path in one structural way, and every
property below follows from it: **signing happens INSIDE the build**. The NSIS
installer is a self-extracting archive -- the app executable is signed, then
compressed into the installer payload, then the installer and its generated
uninstaller are signed -- so signing afterwards would mean unpacking and
rebuilding that structure by hand. Instead
``website/electron/scripts/sign-windows.js`` hooks electron-builder wherever it
would have called ``signtool``.

That makes the Windows build job hold a production signing identity, which the
macOS legs deliberately do not. It is also why Windows has its own reusable
workflow: ``build-desktop.yml`` is pinned credential-free by
``test_workflow_permissions.py``, and putting the signing leg back in it would
hand OIDC to the mac and Linux legs too.

The properties that keep this safe, none of which fails at PR time if broken:

* **``id-token: write`` is granted by the callers.** A reusable workflow can
  never exceed its caller's permissions, so a callee declaring OIDC is not
  enough -- the nightly/release caller jobs must grant it. Pinned in
  ``test_workflow_permissions.py``; asserted here from the callee side.
* **The prod environment is requested on publishing paths.** The signing role's
  OIDC trust accepts exactly ``ref:refs/heads/main`` and ``environment:prod``.
  Release runs are tag-triggered (``ref:refs/tags/v*``, untrusted), so without
  the environment they cannot assume the role. It must NOT be requested on the
  any-ref dispatch probe, whose refs the prod branch policy rejects.
* **The hook is wired into electron-builder.** A ``win`` config without
  ``signtoolOptions.sign`` builds a perfectly good UNSIGNED installer, silently.
* **``CSC_IDENTITY_AUTO_DISCOVERY`` stays false.** There is no certificate on the
  runner -- the private key lives in the signing service and never leaves it.
  Letting electron-builder hunt for a local certificate invites it to pick up
  something unexpected instead of going through the hook.
* **The five ``WINDOWS_SIGNING_*`` values are gated on the same flag as the
  credentials.** They are inline literals, so if they were set unconditionally
  the hook could never reach its "not configured" skip path: it would find a
  full environment, call the AWS CLI without credentials, and FAIL the build on
  every fork and on any repo without the secret. That regression is the reason
  this file asserts the gate and not just the values.

``sts:ExternalId`` must equal the Signer application name, which is undocumented
and load-bearing -- without it any principal in the allowlisted account can
assume the artifact role.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
BUILD_WORKFLOW = WORKFLOWS / "build-windows.yml"
ELECTRON_PACKAGE_JSON = ROOT / "website" / "electron" / "package.json"
SIGN_HOOK = ROOT / "website" / "electron" / "scripts" / "sign-windows.js"

# Values the deployed infrastructure actually uses. Pinned rather than derived so
# a rename on either side has to be a deliberate, visible edit here: the Signer
# application name doubles as the IAM role prefix AND the sts:ExternalId, so
# changing it forces Signer to recreate the signing profiles or every job starts
# failing with AccessDeniedException. Deployed in KiroCrewPublishCDK
# lib/windows-signer-stack.ts.
SIGNER_APPLICATION_NAME = "KiroCrewWindows"
EXPECTED_SIGNING_ENV = {
    "WINDOWS_SIGNING_UNSIGNED_BUCKET": "kirocrew-windows-unsigned-116101834266",
    "WINDOWS_SIGNING_SIGNED_BUCKET": "kirocrew-windows-signed-116101834266",
    "WINDOWS_SIGNING_PROFILE_ID": "KiroCrewWindowsExe",
    "WINDOWS_SIGNING_ARTIFACT_ROLE": (
        f"arn:aws:iam::116101834266:role/{SIGNER_APPLICATION_NAME}-ArtifactAccessRole"
    ),
    "WINDOWS_SIGNING_EXTERNAL_ID": SIGNER_APPLICATION_NAME,
}

# Callers that build Windows, and whether they publish (and so must request the
# prod environment for the signing role's OIDC trust to accept them).
PUBLISHING_CALLERS = ("nightly.yml", "release.yml")


def _workflow(name: str = "build-windows.yml") -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _build_job() -> dict:
    return _workflow()["jobs"]["build-windows"]


def _step(name_fragment: str) -> dict:
    for step in _build_job()["steps"]:
        if name_fragment in step.get("name", ""):
            return step
    raise AssertionError(
        f"no step whose name contains {name_fragment!r} in build-windows.yml; "
        "steps are: " + ", ".join(repr(s.get("name", "")) for s in _build_job()["steps"])
    )


def test_the_callee_requests_an_oidc_token() -> None:
    # No OIDC token, no role, no signature. The caller side (which must also
    # grant it, since a callee cannot exceed its caller) is pinned in
    # test_workflow_permissions.py.
    assert _build_job()["permissions"]["id-token"] == "write"


def test_publishing_callers_request_the_prod_environment() -> None:
    """Without this, release builds cannot assume the signing role at all.

    Release runs are tag-triggered and present ref:refs/tags/v*, which the role
    does not trust; only ref:refs/heads/main and environment:prod are accepted.
    So a missing environment breaks signing on releases while nightly (which
    runs on main) keeps working -- invisible until a release.
    """
    for caller in PUBLISHING_CALLERS:
        job = _workflow(caller)["jobs"]["build-windows"]
        assert job["with"]["use_prod_environment"] is True, (
            f"{caller} must pass use_prod_environment: true, or tag-triggered "
            "runs cannot assume the signing role"
        )


def test_the_environment_is_conditional_so_the_any_ref_probe_survives() -> None:
    """The prod environment must be requestable, not unconditional.

    The prod environment's deployment branch policy allows only main and v*
    tags. A job-level `environment: prod` would therefore fail the any-ref
    workflow_dispatch packaging probe at job start on a feature branch --
    defeating the one mechanism for validating a packaging change before merge.
    """
    environment = _build_job()["environment"]
    assert "inputs.use_prod_environment" in environment, (
        f"environment {environment!r} must derive from the use_prod_environment "
        "input. Note it cannot test github.event_name: inside a called reusable "
        "workflow the github context reflects the CALLER's event, so event_name "
        "is never 'workflow_call' and the environment would never be applied."
    )


def test_the_soft_fail_input_is_declared_on_every_trigger() -> None:
    """A job-level expression reading an input must find it on BOTH triggers.

    `continue-on-error` is evaluated before any job starts. When it reads an
    input a trigger does not declare, the value is empty, the key is not a
    boolean, and GitHub rejects the whole workflow at startup: the run ends in
    seconds with ZERO jobs and no log. That is how the dispatch probe path died
    silently once already in build-desktop.yml.
    """
    text = BUILD_WORKFLOW.read_text(encoding="utf-8")
    referenced = set(re.findall(r"inputs\.([A-Za-z_][A-Za-z0-9_]*)", text))
    assert referenced, "build-windows.yml no longer references any inputs"

    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1), so the
    # trigger block is not reachable under the string "on".
    workflow = _workflow()
    triggers = workflow[True] if True in workflow else workflow["on"]
    for trigger in ("workflow_call", "workflow_dispatch"):
        declared = set((triggers[trigger] or {}).get("inputs", {}))
        missing = referenced - declared
        assert not missing, (
            f"{trigger} does not declare input(s) {sorted(missing)} that a "
            "job-level key reads. GitHub rejects the workflow at startup on "
            "that trigger and the run produces zero jobs."
        )


def test_continue_on_error_is_boolean_safe() -> None:
    """continue-on-error must coerce to a boolean even for an absent input."""
    expr = str(_build_job()["continue-on-error"])
    assert "== true" in expr or "fromJSON" in expr, (
        f"continue-on-error expression {expr!r} yields the input's raw value; an "
        "absent input then makes it non-boolean and GitHub rejects the workflow "
        "at startup. Compare explicitly (`== true`) or coerce with fromJSON."
    )


def test_electron_builder_is_wired_to_the_signing_hook() -> None:
    # The single most important assertion here: a win config without this builds
    # a working but UNSIGNED installer and says nothing about it.
    config = json.loads(ELECTRON_PACKAGE_JSON.read_text(encoding="utf-8"))
    sign = config["build"]["win"]["signtoolOptions"]["sign"]
    assert sign == "./scripts/sign-windows.js"
    assert SIGN_HOOK.is_file(), f"{sign} is configured but {SIGN_HOOK} does not exist"


def test_only_one_hash_algorithm_is_signed() -> None:
    # electron-builder's legacy signtool default is ["sha1", "sha256"], and it
    # invokes the sign hook ONCE PER ALGORITHM. The signing profile is
    # SHA256-only, so an unpinned list makes every file take a second round
    # trip through S3 and the signing service that can only ever reproduce the
    # first signature, doubling both the signing-job count and the build's
    # wall-clock cost.
    config = json.loads(ELECTRON_PACKAGE_JSON.read_text(encoding="utf-8"))
    algorithms = config["build"]["win"]["signtoolOptions"].get("signingHashAlgorithms")
    assert algorithms == ["sha256"], (
        "signingHashAlgorithms must be pinned to exactly ['sha256']; found "
        f"{algorithms!r}. Leaving it unset restores electron-builder's "
        "sha1+sha256 default and signs every artifact twice."
    )


def test_local_certificate_discovery_stays_disabled() -> None:
    # There is no certificate on the runner; signing goes through the hook.
    env = _step("Build desktop app")["env"]
    assert env["CSC_IDENTITY_AUTO_DISCOVERY"] == "false"


def _publish_job() -> dict:
    return _workflow("publish-windows.yml")["jobs"]["publish-windows"]


def _publish_step(name_fragment: str) -> dict:
    for step in _publish_job()["steps"]:
        if name_fragment in step.get("name", ""):
            return step
    raise AssertionError(f"publish-windows.yml has no step named like {name_fragment!r}")


def test_the_publish_lane_expects_the_publisher_the_client_verifies() -> None:
    # NsisUpdater verifies the downloaded installer's Authenticode publisher
    # fail-closed against electron-builder's publisherName. If the publish lane
    # accepts a different CN than the client demands, the lane happily publishes
    # bytes that every client then refuses, and the mutable latest.yml means it
    # refuses them all at once. One value, asserted from both ends.
    #
    # The location is part of the contract, not a detail. electron-builder 26's
    # WindowsConfiguration is `additionalProperties: false` and carries no
    # publisherName -- it belongs to WindowsSigntoolConfiguration, i.e. inside
    # signtoolOptions beside `sign`. A win-level key is not merely ignored: the
    # schema validator rejects the whole config, so EVERY desktop build fails on
    # EVERY platform, mac and Linux included. Only this path is read by
    # WindowsSignToolManager.computedPublisherName, which is what
    # PublishManager copies into app-update.yml, which is the only thing
    # NsisUpdater.verifySignature consults -- and an absent value there makes it
    # return early and skip verification instead of failing, so a misplaced key
    # silently costs the fail-closed check rather than announcing itself.
    expected = _publish_job()["env"]["EXPECT_SUBJECT_CN"]
    config = json.loads(ELECTRON_PACKAGE_JSON.read_text(encoding="utf-8"))
    win = config["build"]["win"]
    assert "publisherName" not in win, (
        "publisherName must live in win.signtoolOptions, not win: electron-builder's "
        "WindowsConfiguration forbids unknown keys and fails every desktop build."
    )
    publisher_name = win["signtoolOptions"]["publisherName"]
    assert publisher_name == [expected], (
        f"publish-windows.yml verifies publisher {expected!r} but the client pins "
        f"{publisher_name!r}; the lane would publish installers the updater rejects."
    )
    # verifyUpdateCodeSignature defaults to on (isForceCodeSigningVerification is
    # `!== false`); setting it false would drop publisherName from app-update.yml
    # and disable the check the publish guard is paired with.
    assert win.get("verifyUpdateCodeSignature") is not False


def test_the_published_basename_matches_the_clients_manual_download_url() -> None:
    # manualDownloadUrl() is the escape hatch a user follows when an in-app
    # update fails, so a basename drift turns the one recovery path into a 404.
    basename = _publish_job()["env"]["PUBLISHED_BASENAME"]
    auto_update = (ROOT / "website" / "electron" / "auto-update.js").read_text(encoding="utf-8")
    assert f'"{basename}.exe"' in auto_update, (
        f"publish-windows.yml publishes {basename}.exe but auto-update.js does not "
        "build that filename; the manual download link would 404."
    )


def test_windows_has_exactly_one_channel_file() -> None:
    # electron-updater's Provider.getChannelFilePrefix() appends an arch suffix
    # for linux only and returns "" for win32, so NsisUpdater requests bare
    # `latest.yml` for EVERY arch. A `latest-<arch>.yml` would be written and
    # never read, and the arm64 client would silently fetch the x64 feed. A
    # second Windows arch therefore means a second entry inside this same file.
    resolve = _publish_step("Resolve published names")["run"]
    assert "FEED_FILE=latest.yml" in resolve
    assert (
        "latest-arm64.yml" not in resolve
    ), "a per-arch Windows feed file is never requested by any client"

    # And there is no `arch` input to request one with. Absence is the guarantee:
    # a validated parameter can still be handed a value this lane does not build,
    # while a parameter that does not exist cannot. publish-linux.yml keeps its
    # `arch` because its callers pass two; both callers here pass none.
    workflow = _workflow("publish-windows.yml")
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert "arch" not in triggers["workflow_call"]["inputs"], (
        "an arch input invites a value this lane cannot publish; a second arch "
        "has to edit this workflow anyway to share the single latest.yml"
    )
    for name in ("nightly.yml", "release.yml"):
        job = next(
            j
            for j in _workflow(name)["jobs"].values()
            if str(j.get("uses", "")).endswith("publish-windows.yml")
        )
        assert "arch" not in job["with"], f"{name} still passes a removed input"


def test_no_reusable_workflow_caller_uses_an_unsupported_key() -> None:
    """`continue-on-error` on a reusable-workflow call breaks the WHOLE workflow.

    GitHub's supported-keyword list for a job that calls a reusable workflow is
    exhaustive -- name, uses, with, secrets, strategy, needs, if, concurrency,
    permissions -- so an unsupported key fails workflow validation and the run
    starts NO jobs at all.

    This is a tripwire rather than hygiene. `continue-on-error` is exactly what a
    reader reaches for on discovering that a hard Windows publish failure makes
    the insider run unsuccessful, which blocks release_promotion.py from
    promoting that commit (it requires `conclusion == "success"`). The tempting
    one-line fix silently costs the entire release workflow, so the constraint is
    pinned here with the reason attached. Recovery for that coupling is
    operational: re-run the failed job and the run concludes success.
    """
    supported = {
        "name",
        "uses",
        "with",
        "secrets",
        "strategy",
        "needs",
        "if",
        "concurrency",
        "permissions",
    }
    for name in ("nightly.yml", "release.yml"):
        for job_name, job in _workflow(name)["jobs"].items():
            if "uses" not in job:
                continue
            unsupported = set(job) - supported
            assert not unsupported, (
                f"{name}:{job_name} calls a reusable workflow with "
                f"{sorted(unsupported)}, which GitHub rejects at workflow "
                "validation -- the run would start no jobs"
            )


def test_the_updater_offers_exactly_the_channels_that_publish_windows() -> None:
    # A Windows client resolving a channel with no lane fetches a feed that was
    # never written: every check 404s and the manual-download link is dead. The
    # channels the client knows and the channels that actually publish Windows
    # have to move together, in both directions.
    #
    # Expressed against KNOWN_CHANNELS rather than a Windows-specific set:
    # publish-windows.yml is now wired into every channel, so a separate set
    # would be a declaration claiming a restriction that does not exist. If a
    # channel ever loses its Windows lane, this fails -- and the fix is to
    # reintroduce the restriction and report `disabled: "channel"`, not to delete
    # the assertion.
    auto_update = (ROOT / "website" / "electron" / "auto-update.js").read_text(encoding="utf-8")
    match = re.search(r"KNOWN_CHANNELS = new Set\(\[([^\]]*)\]\)", auto_update)
    assert match, "auto-update.js no longer declares KNOWN_CHANNELS"
    client_channels = set(re.findall(r'"([^"]+)"', match.group(1)))
    assert "channelHasLane(channel)" in auto_update or "channelHasLane(currentChannel())" in (
        auto_update
    ), "channelHasLane must stay the single place a channel is checked for a lane"

    nightly = next(
        job
        for job in _workflow("nightly.yml")["jobs"].values()
        if str(job.get("uses", "")).endswith("publish-windows.yml")
    )
    assert nightly["with"]["channel"] == "nightly"

    release = next(
        job
        for job in _workflow("release.yml")["jobs"].values()
        if str(job.get("uses", "")).endswith("publish-windows.yml")
    )
    # Both of release.yml's channels publish, and stable must reach the lane
    # through PROMOTION rather than a fresh build: it republishes the bundle
    # resolve-promotion verified, so the caller has to gate on that job and pass
    # promote plus the base version the manifest is checked against.
    assert "channel == 'insider'" in release["if"]
    assert "channel == 'stable'" in release["if"]
    assert "needs.resolve-promotion.result == 'success'" in release["if"]
    assert "resolve-promotion" in release["needs"]
    assert "channel == 'stable'" in release["with"]["promote"]
    assert release["with"]["promotion_base_version"], "stable promotion needs a base version"
    # The stable installer comes from the verified handoff artifact, never from a
    # fresh build-windows upload.
    assert "KiroCrew-notarized-stable-" in release["with"]["installer_artifact"]

    published = {nightly["with"]["channel"]} | {"insider", "stable"}
    assert client_channels == published, (
        f"the client knows channels {sorted(client_channels)} but the workflows "
        f"publish Windows on {sorted(published)}; a channel on only one side "
        "either offers a dead updater or hides a lane that exists"
    )


def test_the_artifact_probe_retries_a_blip_but_still_fails_closed() -> None:
    """Both halves matter, and they pull against each other.

    Failing closed is correct: an unknown is not an absence, and treating a failed
    listing as "nothing to publish" would silently skip a release. But this job
    hard-failing makes the whole insider run unsuccessful, and
    release_promotion.py then refuses to promote that commit at all -- so a single
    Actions-API blip during an insider run would cost stable promotion of the mac,
    Linux and CLI artifacts that same run already published.

    Retrying absorbs the blip without weakening the guarantee: a sustained failure
    still stops the lane. Dropping either half is a regression, so both are pinned
    here.
    """
    probe = _publish_step("Probe for the installer artifact")["run"]
    assert "for attempt in" in probe, "a transient listing failure must be retried"
    assert "sleep" in probe, "retries need to back off, not hammer the API"
    assert "::error::" in probe and "exit 1" in probe, (
        "a sustained listing failure must still fail closed rather than be "
        "laundered into nothing-to-publish"
    )
    # The absence branch must stay reachable: a build that genuinely produced no
    # installer is a clean skip, which is what keeps a Windows-only failure from
    # blocking the other platforms' lanes.
    assert "present=" in probe and "::notice::" in probe


def test_the_signature_is_verified_before_the_bytes_become_immutable() -> None:
    # The versioned key is written with --if-none-match, so an unsigned or
    # wrongly-signed installer that reaches S3 burns that version string. The
    # guard is only a guard if it runs first.
    names = [step.get("name", "") for step in _publish_job()["steps"]]
    verify = next(i for i, name in enumerate(names) if "Authenticode" in name)
    publish = next(i for i, name in enumerate(names) if "Publish installer" in name)
    attest = next(i for i, name in enumerate(names) if "Attest installer" in name)
    assert verify < attest < publish, (
        f"expected verify({verify}) < attest({attest}) < publish({publish}); an "
        "un-verified or un-attested installer must never reach an immutable key."
    )


def test_publishing_callers_consume_the_artifact_the_build_uploads() -> None:
    # A typo here is silent: publish-windows.yml probes for the artifact and
    # skips cleanly when it is absent, so a mismatched name reads as "the build
    # produced nothing" and Windows quietly stops publishing.
    upload_name = _step("Upload desktop artifact")["with"]["name"]
    for caller in PUBLISHING_CALLERS:
        jobs = _workflow(caller)["jobs"]
        publish_jobs = [
            job for job in jobs.values() if str(job.get("uses", "")).endswith("publish-windows.yml")
        ]
        assert publish_jobs, f"{caller} does not call publish-windows.yml"
        for job in publish_jobs:
            consumed = job["with"]["installer_artifact"]
            if "${{" not in consumed:
                assert consumed == upload_name, (
                    f"{caller} consumes {consumed!r} but "
                    f"build-windows.yml uploads {upload_name!r}"
                )
                continue
            # release.yml selects between the verified stable handoff and this
            # run's fresh build. Only the fresh-build branch is checkable
            # against build-windows.yml, and it is the branch a typo hides in --
            # the promotion branch is generated from the version and would fail
            # loudly at download time instead of skipping.
            assert (
                f"|| '{upload_name}'" in consumed
            ), f"{caller}'s fresh-build branch does not name {upload_name!r}: {consumed!r}"
            assert "KiroCrew-notarized-stable-" in consumed, (
                f"{caller}'s promotion branch must consume the verified handoff "
                f"artifact, not a fresh build: {consumed!r}"
            )


def test_all_five_signing_env_vars_carry_the_deployed_values() -> None:
    # The hook needs ALL five; the values must match the deployed
    # infrastructure or every signing job fails with AccessDeniedException.
    env = _step("Build desktop app")["env"]
    actual = {k: v for k, v in env.items() if k.startswith("WINDOWS_SIGNING_")}
    assert set(actual) == set(EXPECTED_SIGNING_ENV)
    for name, expected in EXPECTED_SIGNING_ENV.items():
        assert expected in actual[name], f"{name} must carry {expected!r}; found {actual[name]!r}"


def test_the_five_env_values_are_gated_on_the_signing_secret() -> None:
    """The regression this file exists to prevent.

    The five values are inline literals. Set unconditionally, the hook's
    "not configured -> skip" path becomes unreachable: on a fork, or on any repo
    without the signing secret, the credential step skips but the hook still
    finds a complete environment, shells out to the AWS CLI with no credentials,
    and FAILS the Windows build. Gating them on the same flag as the credential
    step is what makes the documented skip behaviour real.
    """
    env = _step("Build desktop app")["env"]
    for name in EXPECTED_SIGNING_ENV:
        assert "HAS_WINDOWS_SIGNING" in str(env[name]), (
            f"{name} is set unconditionally. It must be gated on "
            "HAS_WINDOWS_SIGNING, or an unconfigured build fails instead of "
            "shipping an unsigned installer."
        )


def test_external_id_equals_the_signer_application_name() -> None:
    # Undocumented and load-bearing: ArtifactAccessRole's trust policy requires
    # sts:ExternalId = the application name. Without it, anything in the
    # allowlisted account can assume the role.
    env = _step("Build desktop app")["env"]
    assert SIGNER_APPLICATION_NAME in str(env["WINDOWS_SIGNING_EXTERNAL_ID"])
    assert SIGNER_APPLICATION_NAME in str(env["WINDOWS_SIGNING_ARTIFACT_ROLE"])


def test_credentials_are_configured_before_the_build_runs() -> None:
    # Signing happens during the build, so the role must already be assumed.
    names = [s.get("name", "") for s in _build_job()["steps"]]
    cred = next(i for i, n in enumerate(names) if "Configure AWS credentials" in n)
    build = names.index("Build desktop app")
    assert cred < build, "AWS credentials must be configured before the build signs anything"


def test_credential_step_skips_without_the_secret() -> None:
    # Gated on the secret so forks build unsigned instead of failing. No
    # per-OS condition is needed any more: this workflow is Windows-only.
    assert "HAS_WINDOWS_SIGNING" in _step("Configure AWS credentials")["if"]


def test_signing_gate_is_hoisted_into_job_env() -> None:
    # `secrets.*` is not available in a step-level `if`, so the gate has to be a
    # job-level env flag. Same pattern as sign-and-notarize.yml.
    job_env = _build_job()["env"]
    assert "AWS_WINDOWS_SIGNING_ROLE_ARN" in job_env["HAS_WINDOWS_SIGNING"]


def test_the_signing_gate_also_requires_the_prod_environment() -> None:
    """Signing needs the secret AND the environment, so the gate needs both.

    The role trusts only ref:refs/heads/main and environment:prod. Gating on the
    secret alone means that the moment the secret is added, the any-ref dispatch
    probe starts trying to assume the role from an untrusted feature-branch ref
    and dies at the credential step -- killing the probe the conditional
    environment exists to protect. A probe is supposed to build unsigned.
    """
    gate = _build_job()["env"]["HAS_WINDOWS_SIGNING"]
    assert "use_prod_environment" in gate, (
        f"HAS_WINDOWS_SIGNING ({gate!r}) must also require use_prod_environment, "
        "or adding the signing secret breaks the any-ref packaging probe."
    )
    assert "\n" not in gate, (
        "keep the gate on one line: a folded block scalar preserves newlines for "
        "more-indented continuation lines, which corrupts the expression"
    )


def test_the_pairing_guard_runs_before_the_upload_and_fails_hard() -> None:
    """An orphaned installer must never be uploaded (#4301).

    The guard exists so an .exe with no .blockmap beside it fails the build
    BEFORE the artifact is produced: the publish lane then takes its
    documented build-produced-nothing skip path instead of hard-failing an
    insider run, which would block stable promotion of the mac, Linux and CLI
    artifacts (the coupling soft_fail exists to prevent). Nothing else fails
    at PR time if the step is deleted or reordered, so it is pinned here like
    the other load-bearing properties of this workflow.
    """
    guard = _step("installer/blockmap")
    run = guard["run"]
    assert "exit 1" in run, "the guard must fail the job, not merely annotate"
    assert "::error::" in run, "the failure must annotate the run"
    assert "GITHUB_STEP_SUMMARY" in run, (
        "soft_fail keeps the run green, so the reason must reach the run "
        "summary page rather than living only in the job log"
    )
    names = [s.get("name", "") for s in _build_job()["steps"]]
    guard_i = next(i for i, n in enumerate(names) if "installer/blockmap" in n)
    upload_i = names.index("Upload desktop artifact")
    build_i = names.index("Build desktop app")
    assert build_i < guard_i < upload_i, (
        f"expected build({build_i}) < guard({guard_i}) < upload({upload_i}): "
        "the guard must inspect the built dist and refuse the upload"
    )


def test_artifact_paths_contain_no_yaml_comments() -> None:
    """`path:` is a block scalar, where a '#' line is a glob, not a comment.

    Every line of a `|` block is literal text, so a comment written inside
    `path:` silently becomes a pattern that matches nothing. upstream-artifact
    only errors when NO pattern matches, so the mistake stays invisible while
    quietly widening the set of things that must keep matching.
    """
    upload = _step("Upload desktop artifact")
    offenders = [
        line.strip() for line in upload["with"]["path"].splitlines() if line.strip().startswith("#")
    ]
    assert not offenders, (
        f"comment lines inside `path:` are treated as glob patterns: {offenders}. "
        "Move them above the step."
    )


def test_the_hook_pins_the_aws_cli_output_format() -> None:
    """The tag poll parses the CLI's stdout, so the format cannot be ambient.

    `--output` defaults from config: AWS_DEFAULT_OUTPUT, or `output = text` in a
    runner image's ~/.aws/config. Under `text`, JSON.parse throws -- and it
    throws OUTSIDE the try that guards the call, so it would abort a signing
    build rather than retry.
    """
    source = SIGN_HOOK.read_text(encoding="utf-8")
    assert "'--output', 'json'" in source or '"--output", "json"' in source, (
        "pin --output json in the aws() helper: the tag poll JSON.parses stdout, "
        "and the CLI's output format is otherwise ambient configuration"
    )


def test_the_hook_refuses_a_partially_configured_environment() -> None:
    """Skipping on a partial environment would be the worst available outcome.

    Someone who wired signing up on purpose but missed one variable would get a
    silently unsigned installer that reports success. The hook throws instead.
    """
    source = SIGN_HOOK.read_text(encoding="utf-8")
    assert "missing.length === REQUIRED_ENV.length" in source, (
        "sign-windows.js must distinguish a fully-unconfigured environment "
        "(skip) from a partial one (throw)"
    )
    assert re.search(
        r"missing\.length > 0[\s\S]{0,400}throw new Error", source
    ), "a partially configured environment must throw, not skip"


def test_promotion_verifies_the_whole_bundle_before_reading_the_installer() -> None:
    # A stable publish republishes bytes recorded at insider time, so the
    # candidate's integrity is the thing to establish first. Verifying only the
    # installer would accept a bundle whose OTHER files were swapped, and this
    # lane is one of the readers that would then trust it.
    verify = _publish_step("Verify immutable promotion bundle")
    assert "inputs.promote" in verify["if"]
    assert "scripts/release_promotion.py verify" in verify["run"]
    assert '--expected-source-sha "${GITHUB_SHA}"' in verify["run"]
    assert '--expected-base-version "${PROMOTION_BASE_VERSION}"' in verify["run"]

    names = [step.get("name", "") for step in _publish_job()["steps"]]
    assert names.index("Verify immutable promotion bundle") < names.index(
        "Locate the installer and its blockmap"
    )


def test_a_promoted_candidate_without_an_installer_skips_but_a_fresh_build_fails() -> None:
    # These two halves pull against each other and neither may be dropped.
    #
    # SKIP: the Windows promotion role is optional, so a candidate recorded from
    # a run whose Windows build failed carries no installer. Failing there would
    # let one platform's build problem block the stable release of every other
    # platform -- exactly what optionality buys back.
    #
    # FAIL: in fresh-build mode the artifact was probed and found, so a missing
    # .exe inside it means the artifact's shape changed. Skipping there would
    # silently stop publishing Windows with a green run.
    run = _publish_step("Locate the installer and its blockmap")["run"]
    # The caller-fed promote flag reaches the script through env:, never
    # interpolated into it. `${{ }}` expanded inside a run: block is a shell
    # injection surface (Semgrep run-shell-injection), and this step is the one
    # place in the lane that has to branch on a caller input.
    locate = _publish_step("Locate the installer and its blockmap")
    assert locate["env"]["PROMOTE"] == "${{ inputs.promote }}"
    assert "${{" not in run, f"run: block interpolates a workflow expression: {run!r}"
    assert 'if [ "${#FOUND[@]}" -eq 0 ] && [ "${PROMOTE}" = "true" ]' in run
    assert "staged=" in run and "::notice::" in run
    assert 'if [ "${#FOUND[@]}" -ne 1 ]' in run
    assert "::error::expected exactly one .exe" in run
    # The blockmap requirement holds in BOTH modes: an installer published
    # without it degrades every client to a full download, silently.
    assert '[ ! -f "${SRC}.blockmap" ]' in run


def test_promotion_reverifies_provenance_instead_of_minting_a_second_attestation() -> None:
    # Attesting republished bytes would testify only that stable's own run held
    # the file, which is equally true of tampered bytes. The original attestation
    # from the insider publish is the one that means something.
    attest = _publish_step("Attest installer provenance")
    reverify = _publish_step("Verify promoted installer provenance")
    assert "!inputs.promote" in attest["if"], "a promoted republish must not re-attest"
    assert "inputs.promote" in reverify["if"] and "!inputs.promote" not in reverify["if"]
    assert "gh attestation verify" in reverify["run"]
    # Pinned to THIS workflow: any attestation signed by a different workflow is
    # not the one this lane produced when the bytes were first published.
    assert "--signer-workflow" in reverify["run"]
    assert "publish-windows.yml" in reverify["run"]


def test_every_publishing_step_is_gated_on_staged_bytes_not_on_the_probe() -> None:
    # The probe answers "did this run upload the artifact", which in promote mode
    # is always yes -- resolve-promotion uploaded the bundle. Only the locate step
    # knows whether an INSTALLER came out of it, so everything that touches the
    # bucket or the feed has to hang off that, or a candidate without a Windows
    # installer would publish a feed pointing at bytes that were never staged.
    publishing = (
        "Verify the Authenticode signature",
        "Attest installer provenance",
        "Verify promoted installer provenance",
        "Publish installer to distribution bucket",
        "Write update feed",
        "Update latest installer alias",
    )
    for name in publishing:
        condition = _publish_step(name)["if"]
        assert "steps.locate.outputs.staged" in condition, f"{name} is not gated on staged bytes"
        assert "steps.probe.outputs.present" not in condition, (
            f"{name} still gates on the probe, which cannot see whether the "
            "promoted bundle carried an installer"
        )
