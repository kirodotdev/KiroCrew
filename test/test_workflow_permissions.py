"""Regression tests for least-privilege GitHub workflow permissions."""

from __future__ import annotations

from pathlib import Path

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
        must never hold OIDC or write capabilities."""
        assert _workflow_permissions("build-wheel.yml") == {"contents": "read"}
        assert _workflow_permissions("build-desktop.yml") == {"contents": "read"}

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
            "issues": "write",
            "pull-requests": "read",
        }
