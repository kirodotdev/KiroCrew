"""Regression tests for least-privilege GitHub workflow permissions."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _lines(name: str) -> list[str]:
    return (WORKFLOWS / name).read_text(encoding="utf-8").splitlines()


def _permission_block(lines: list[str], marker: str) -> dict[str, str] | None:
    """Return the permissions nested directly under an exact YAML marker."""
    start = lines.index(marker)
    marker_indent = len(marker) - len(marker.lstrip())

    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= marker_indent:
            break
        if line.strip() != "permissions:" or indent != marker_indent + 2:
            continue

        permissions: dict[str, str] = {}
        for permission_line in lines[index + 1 :]:
            if not permission_line.strip():
                continue
            permission_indent = len(permission_line) - len(permission_line.lstrip())
            if permission_indent <= indent:
                break
            # Comments are annotation, not grants. Skipping them keeps a
            # documented permissions block readable as a dict; without this the
            # split below raises ValueError and an ordinary assertion failure
            # arrives as an unpackaging crash that names no workflow.
            if permission_line.strip().startswith("#"):
                continue
            key, value = permission_line.strip().split(":", 1)
            permissions[key] = value.strip()
        return permissions

    return None


def _workflow_permissions(name: str) -> dict[str, str]:
    lines = _lines(name)
    start = lines.index("permissions:")
    permissions: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        if not line.startswith("  "):
            break
        # See _permission_block: comments must not be parsed as grants.
        if line.strip().startswith("#"):
            continue
        key, value = line.strip().split(":", 1)
        permissions[key] = value.strip()
    return permissions


class TestNightlyPermissions:
    def test_only_publish_callers_can_mint_oidc_tokens(self) -> None:
        lines = _lines("nightly.yml")

        assert _workflow_permissions("nightly.yml") == {"contents": "read"}
        assert _permission_block(lines, "  version:") is None
        # Build callers inherit the workflow-level contents:read only; the
        # reusable build workflows request nothing more.
        assert _permission_block(lines, "  build-wheel:") is None
        assert _permission_block(lines, "  build-desktop:") is None
        # The Windows build signs during the build, so unlike build-desktop it
        # must be granted OIDC explicitly (a callee can never exceed its
        # caller). Never contents:write: it holds a signing identity.
        assert _permission_block(lines, "  build-windows:") == {
            "contents": "read",
            "id-token": "write",
        }
        assert _permission_block(lines, "  publish-cli:") == {
            "contents": "read",
            "id-token": "write",
            "attestations": "write",
        }
        # The Linux desktop lane publishes S3 objects (OIDC) and attests
        # its own SLSA provenance for the exact bytes it uploads -- never
        # contents:write.
        assert _permission_block(lines, "  publish-linux:") == {
            "contents": "read",
            "id-token": "write",
            "attestations": "write",
        }
        # The Docker lane pushes to ghcr.io with the workflow's own
        # GITHUB_TOKEN: packages:write is required for the push and MUST
        # stay scoped to this caller job (never workflow-level -- a
        # registry-poisoning capability in every job would defeat the
        # least-privilege split). id-token + attestations cover the in-lane
        # SLSA provenance push; never contents:write.
        assert _permission_block(lines, "  publish-docker:") == {
            "contents": "read",
            "packages": "write",
            "id-token": "write",
            "attestations": "write",
        }
        # Caller job for the reusable sign-and-notarize workflow: a
        # workflow_call callee can never exceed the caller job's permissions,
        # so the caller must grant id-token explicitly. attestations:write
        # covers the sign job's wheel/sdist/AppImage provenance and the
        # notarize job's shipping-DMG attestation.
        assert _permission_block(lines, "  sign-and-notarize:") == {
            "id-token": "write",
            "contents": "read",
            "attestations": "write",
        }


class TestReleasePermissions:
    def test_release_jobs_follow_least_privilege_split(self) -> None:
        """The signing caller holds AWS creds (id-token) but must not hold
        contents:write; the GitHub-Release job holds contents:write but must
        not hold AWS creds. Keeping the two capabilities in separate jobs
        means a compromise of either job cannot both exfiltrate via AWS and
        tamper with the repo/release."""
        lines = _lines("release.yml")

        assert _workflow_permissions("release.yml") == {"contents": "read"}
        assert _permission_block(lines, "  version:") is None
        assert _permission_block(lines, "  build-wheel:") is None
        assert _permission_block(lines, "  build-desktop:") is None
        # Windows build: OIDC for signing, never contents:write (see nightly).
        assert _permission_block(lines, "  build-windows:") == {
            "contents": "read",
            "id-token": "write",
        }
        assert _permission_block(lines, "  publish-cli:") == {
            "contents": "read",
            "id-token": "write",
            "attestations": "write",
        }
        # Linux desktop lane: OIDC + in-lane provenance (see nightly note).
        assert _permission_block(lines, "  publish-linux:") == {
            "contents": "read",
            "id-token": "write",
            "attestations": "write",
        }
        # Docker lane: ghcr.io push via GITHUB_TOKEN (packages:write scoped
        # to this job only) + in-lane provenance (see nightly note).
        assert _permission_block(lines, "  publish-docker:") == {
            "contents": "read",
            "packages": "write",
            "id-token": "write",
            "attestations": "write",
        }
        assert _permission_block(lines, "  sign-and-notarize:") == {
            "id-token": "write",
            "contents": "read",
            "attestations": "write",
        }
        assert _permission_block(lines, "  github-release:") == {
            "contents": "write",
        }


class TestReusableWorkflowPermissions:
    def test_build_workflows_are_read_only(self) -> None:
        """The shared build workflows compile source into artifacts; they
        must never hold OIDC or write capabilities.

        build-windows.yml is deliberately NOT in this list: it Authenticode-signs
        during the build (a Squirrel installer embeds its own already-signed
        executables, so signing cannot be a downstream job) and therefore needs
        OIDC. Keeping it a separate workflow is what lets these two stay
        credential-free -- putting the Windows leg back into build-desktop.yml
        would hand OIDC to the mac and Linux legs as well. See
        test_build_windows_isolates_the_signing_capability.
        """
        assert _workflow_permissions("build-wheel.yml") == {"contents": "read"}
        assert _workflow_permissions("build-desktop.yml") == {"contents": "read"}

    def test_build_desktop_has_no_windows_leg(self) -> None:
        """The credential-free build workflow must not build Windows.

        This is the structural half of the least-privilege split: if a Windows
        leg reappears here it would need OIDC in this workflow, and the
        assertion above would have to be weakened to allow it. Pinning the
        matrix keeps that pressure visible in review instead of arriving as a
        one-line permissions edit.
        """
        # Assert on the resolved matrix, not on the text: the word "windows"
        # legitimately appears in prose, and a substring check would either
        # fire on a comment or be silently defeated by one.
        workflow = yaml.safe_load(
            (WORKFLOWS / "build-desktop.yml").read_text(encoding="utf-8")
        )
        runners = {
            entry["os"]
            for entry in workflow["jobs"]["build-desktop"]["strategy"]["matrix"]["include"]
        }
        assert not any("windows" in runner for runner in runners), (
            f"build-desktop.yml grew a Windows leg (matrix: {sorted(runners)}). "
            "Windows signs during its build and needs OIDC; it belongs in "
            "build-windows.yml so this workflow can stay contents:read only."
        )

    def test_build_windows_isolates_the_signing_capability(self) -> None:
        """The Windows build needs OIDC and nothing more.

        contents:write in particular must never appear: this job holds a
        production signing identity, which is precisely why it is not allowed to
        also mutate the repository.
        """
        assert _workflow_permissions("build-windows.yml") == {"contents": "read"}
        lines = _lines("build-windows.yml")
        assert _permission_block(lines, "  build-windows:") == {
            "contents": "read",
            "id-token": "write",
        }

    def test_sign_and_notarize_declares_exact_capabilities(self) -> None:
        """The shared sign/notarize workflow needs OIDC (AWS signing role)
        and attestations (provenance for the artifacts + shipping DMG) --
        and nothing else. contents:write in particular must never appear
        here (least-privilege split: the GitHub-Release job in release.yml
        is the only writer)."""
        assert _workflow_permissions("sign-and-notarize.yml") == {
            "contents": "read",
            "id-token": "write",
            "attestations": "write",
        }

    def test_publish_cli_declares_exact_capabilities(self) -> None:
        assert _workflow_permissions("publish-cli.yml") == {
            "contents": "read",
            "id-token": "write",
            "attestations": "write",
        }

    def test_publish_docker_declares_exact_capabilities(self) -> None:
        """The Docker lane pushes to ghcr.io (packages:write via
        GITHUB_TOKEN) and attests in-lane SLSA provenance (id-token +
        attestations, pushed to the registry). contents stays read-only;
        no AWS-facing capability exists in this lane at all."""
        assert _workflow_permissions("publish-docker.yml") == {
            "contents": "read",
            "packages": "write",
            "id-token": "write",
            "attestations": "write",
        }


class TestAiReviewOverridePermissions:
    def test_override_handler_has_only_review_control_permissions(self) -> None:
        """The trusted comment handler can re-run reviews and update their
        checks/comments, but must never inherit model credentials or repository
        contents write access."""
        assert _workflow_permissions("ai-review-human-override.yml") == {
            "actions": "write",
            "checks": "write",
            "contents": "read",
            "pull-requests": "write",
        }

    def test_arbiter_model_and_dispatch_capabilities_are_separate(self) -> None:
        lines = _lines("longterm-arbiter.yml")

        assert _workflow_permissions("longterm-arbiter.yml") == {"contents": "read"}
        assert _permission_block(lines, "  arbiter:") == {
            "contents": "read",
            "pull-requests": "write",
            "checks": "write",
            "id-token": "write",
        }
        assert _permission_block(lines, "  refresh-readiness:") == {
            "actions": "write",
            "contents": "read",
        }

    def test_readiness_has_only_aggregation_and_label_permissions(self) -> None:
        assert _workflow_permissions("pr-readiness.yml") == {
            "actions": "read",
            "checks": "read",
            "contents": "read",
            "issues": "write",
            "pull-requests": "write",
            "statuses": "write",
        }
