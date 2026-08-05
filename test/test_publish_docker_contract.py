"""Contract tests for the publish-docker lane's provenance discipline.

The lane's documented guarantees:

* A CHANNEL alias (nightly/insider/stable/latest) never points at a digest
  without registry-pushed SLSA provenance from this repository.
* The workflow never SIGNS bytes it did not build: attestation is created
  only on the fresh-build path; the existing-tag re-run path instead
  VERIFIES that the already-published digest carries this repo's
  provenance, and fails the job (never moving the alias) when it does not.
* A VERSION tag, once published, is never rebuilt or repointed — and a
  transient registry failure must never be misread as "tag absent" (that
  would rebuild different bytes under a published version).
* Every canonical caller requires the anonymous-pull gate, so a GHCR
  visibility regression fails the lane instead of shipping an image the
  documented ``docker pull`` cannot reach.
* The lane needs no repository secrets beyond the implicit GITHUB_TOKEN —
  callers must not ``secrets: inherit`` into it.

Each is pinned structurally here because a plausible-looking edit (e.g.
swapping the attest gate, or "simplifying" the inspect error handling back
to ``if cmd 2>/dev/null``) silently re-opens a provenance or immutability
hole that only manifests on rare re-run/outage paths CI never exercises.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "publish-docker.yml"
CALLERS = (
    ROOT / ".github" / "workflows" / "nightly.yml",
    ROOT / ".github" / "workflows" / "release.yml",
)


def _lines() -> list[str]:
    return WORKFLOW.read_text(encoding="utf-8").splitlines()


def _step_index(lines: list[str], name_prefix: str) -> int:
    for i, line in enumerate(lines):
        if line.strip().startswith("- name: ") and name_prefix in line:
            return i
    raise AssertionError(f"step not found: {name_prefix!r}")


def _step_body(lines: list[str], start: int) -> list[str]:
    """Lines of one step: from its ``- name:`` to the next ``- name:``."""
    body = []
    for line in lines[start + 1 :]:
        if line.strip().startswith("- name: "):
            break
        body.append(line)
    return body


def test_attest_runs_only_for_fresh_builds() -> None:
    """The trusted workflow must never create provenance for a digest it
    did not build in this run — a pre-seeded version tag would otherwise
    acquire fraudulent provenance and pass `gh attestation verify`."""
    lines = _lines()
    attest = _step_index(lines, "Attest image provenance")
    body = _step_body(lines, attest)
    assert any(
        line.strip() == "if: steps.existing.outputs.exists == 'false'"
        for line in body
    ), "attestation must be gated to the fresh-build path"


def test_existing_tag_path_verifies_prior_provenance() -> None:
    """The re-run path must VERIFY the existing digest already carries this
    repo's attestation and hard-fail otherwise — without this, a first run
    that died between its tag push and attest step would launder a
    never-attested digest into the channel alias on re-run."""
    lines = _lines()
    verify = _step_index(lines, "Verify existing digest provenance")
    body = _step_body(lines, verify)
    text = "\n".join(body)
    assert any(
        line.strip() == "if: steps.existing.outputs.exists == 'true'"
        for line in body
    ), "verification is the existing-tag counterpart of the fresh-build attest"
    assert "gh attestation verify" in text
    assert "--signer-workflow" in text, (
        "verification must be bound to THIS workflow's identity, not merely "
        "any attestation from the repo — otherwise a pre-seeded tag pointing "
        "at a digest attested by a different workflow would pass"
    )
    assert "exit 1" in text, "missing provenance must fail the job"
    # And it must guard the alias: verify runs before the alias step.
    alias = _step_index(lines, "Update channel alias")
    assert verify < alias


def test_attest_and_verify_precede_channel_alias_on_selected_digest() -> None:
    """Ordering: select digest -> attest (fresh) / verify (re-run) -> alias.
    The alias step must move only AFTER provenance exists for the exact
    digest it publishes."""
    lines = _lines()
    digest = _step_index(lines, "Select published digest")
    attest = _step_index(lines, "Attest image provenance")
    alias = _step_index(lines, "Update channel alias")
    assert digest < attest < alias

    attest_body = "\n".join(_step_body(lines, attest))
    alias_body = "\n".join(_step_body(lines, alias))
    assert "steps.digest.outputs.value" in attest_body, (
        "attest must sign the SELECTED digest, not steps.build.outputs.digest"
    )
    assert "steps.digest.outputs.value" in alias_body, (
        "the alias must point at the same selected digest provenance covers"
    )


def test_version_tag_build_still_skipped_on_rerun() -> None:
    """The immutable-version discipline stays: the BUILD is what the
    existing-tag check skips."""
    lines = _lines()
    build = _step_index(lines, "Build and push (version tag)")
    body = _step_body(lines, build)
    assert any(
        line.strip() == "if: steps.existing.outputs.exists == 'false'"
        for line in body
    ), "re-runs must never rebuild/republish different bytes under an existing version tag"


def test_channel_alias_moves_only_on_fresh_builds() -> None:
    """Re-running an OLD release workflow hits the existing-tag path; if the
    alias step ran there it would repoint nightly/stable/latest BACKWARD to
    the stale digest for every unpinned pull. The alias must be gated to
    fresh builds."""
    lines = _lines()
    alias = _step_index(lines, "Update channel alias")
    body = _step_body(lines, alias)
    assert any(
        line.strip() == "if: steps.existing.outputs.exists == 'false'"
        for line in body
    ), "the channel alias must never move on the existing-tag re-run path"


def test_rerun_reconcile_only_converges_latest_toward_owned_stable() -> None:
    """The single alias write permitted on a re-run is the stable->latest
    divergence repair, and it must be gated on `stable` ALREADY resolving
    to this run's digest — that precondition is what makes it a
    convergence (interrupted first publish) and never a rollback (re-run
    of an old release, where stable points elsewhere)."""
    lines = _lines()
    reconcile = _step_index(lines, "Reconcile aliases (re-run)")
    body = _step_body(lines, reconcile)
    text = "\n".join(body)
    assert any(
        line.strip() == "if: steps.existing.outputs.exists == 'true'"
        for line in body
    )
    assert '"${STABLE_DIGEST}" = "${DIGEST}"' in text, (
        "the repair must require stable to already own this digest"
    )
    assert "imagetools create -t \"${IMAGE}:latest\"" in text
    # And it must never execute a channel-tag write on this path (the only
    # quoted create target is latest; the channel tag appears solely inside
    # the manual-repair notice text).
    assert "create -t \"${IMAGE}:${CHANNEL}\"" not in text, (
        "the reconcile step may only touch latest, never the channel alias"
    )


def test_existing_tag_check_distinguishes_not_found_from_transport_failure() -> None:
    """Only an explicit registry not-found may select the build path. A
    bare ``if cmd 2>/dev/null`` conflates transient transport/auth failures
    with absence — the failure mode is a rebuild pushing different bytes
    under an already-published version tag during a registry blip."""
    lines = _lines()
    check = _step_index(lines, "Check for existing version tag")
    text = "\n".join(_step_body(lines, check))
    assert "manifest unknown" in text and "name unknown" in text, (
        "the check must classify the inspect error before declaring the tag absent"
    )
    assert "exit 1" in text, (
        "an unclassifiable inspect failure must fail the job, not select the build path"
    )
    assert "2>/dev/null" not in text, (
        "stderr carries the classification signal and must not be discarded"
    )


def test_public_access_gate_is_required_by_every_canonical_caller() -> None:
    """Anonymous pullability is a release invariant for the canonical repo.

    GHCR creates packages private and never inherits visibility from the
    linked repository, so a public repo does not imply a pullable image. The
    README tells users to ``docker pull`` anonymously, so every canonical
    caller must arm the gate — otherwise a visibility regression ships
    silently and the step merely reports ``skipped``.

    The reusable workflow's input still DEFAULTS to false: forks legitimately
    keep private packages, and the gate is additionally scoped to the
    canonical owner.
    """
    import yaml

    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    inputs = workflow[True]["workflow_call"]["inputs"]
    assert inputs["require_public_access"]["default"] is False, (
        "the input must stay opt-in so forks with private packages are "
        "unaffected; canonical callers opt in explicitly"
    )

    lines = _lines()
    public_gate = _step_index(lines, "Verify anonymous pull")
    gate_body = _step_body(lines, public_gate)
    assert any(
        "inputs.require_public_access" in line
        for line in gate_body
        if line.strip().startswith("if:")
    ), "anonymous pull verification must run only when public access is required"

    for caller in CALLERS:
        doc = yaml.safe_load(caller.read_text(encoding="utf-8"))
        docker_jobs = {
            name: job
            for name, job in doc["jobs"].items()
            if "publish-docker.yml" in str(job.get("uses", ""))
        }
        assert docker_jobs, f"{caller.name}: publish-docker call site not found"
        for name, job in docker_jobs.items():
            assert job["with"]["require_public_access"] is True, (
                f"{caller.name}: job {name!r} must require anonymous pull — "
                "the published image is documented as publicly pullable, so a "
                "private package is a broken release, not a valid posture"
            )


def test_callers_do_not_inherit_secrets_into_the_lane() -> None:
    """The lane authenticates with the implicit GITHUB_TOKEN only. Callers
    passing ``secrets: inherit`` would expose every repo secret (signing,
    CDN) to a workflow documented as needing none. Parsed structurally —
    an indentation-based line scan here previously never reached its own
    assertion."""
    import yaml

    for caller in CALLERS:
        doc = yaml.safe_load(caller.read_text(encoding="utf-8"))
        docker_jobs = {
            name: job
            for name, job in doc["jobs"].items()
            if "publish-docker.yml" in str(job.get("uses", ""))
        }
        assert docker_jobs, f"{caller.name}: publish-docker call site not found"
        for name, job in docker_jobs.items():
            assert "secrets" not in job, (
                f"{caller.name}: job {name!r} must not pass secrets into the "
                "docker lane (GITHUB_TOKEN is implicit)"
            )
