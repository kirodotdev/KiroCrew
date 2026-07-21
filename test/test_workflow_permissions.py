"""Regression tests for least-privilege GitHub workflow permissions."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _lines(name: str) -> list[str]:
    return (WORKFLOWS / name).read_text().splitlines()


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
    def test_only_publish_jobs_can_mint_oidc_tokens(self) -> None:
        lines = _lines("nightly.yml")

        assert _workflow_permissions("nightly.yml") == {"contents": "read"}
        assert _permission_block(lines, "  version:") is None
        assert _permission_block(lines, "  build-wheel:") is None
        assert _permission_block(lines, "  build-desktop:") is None
        assert _permission_block(lines, "  publish:") == {
            "id-token": "write",
            "contents": "read",
            "attestations": "write",
        }
        assert _permission_block(lines, "  publish-cli:") == {
            "contents": "read",
            "id-token": "write",
            "attestations": "write",
        }
        # Caller job for the reusable notarize workflow: a workflow_call
        # callee can never exceed the caller job's permissions, so the
        # caller must grant id-token explicitly. attestations:write is for
        # the shipping-DMG provenance attestation inside notarize-macos.yml
        # (the DMG is rebuilt there from the stapled app; the build-job DMG
        # never ships and is attested nowhere).
        assert _permission_block(lines, "  notarize:") == {
            "id-token": "write",
            "contents": "read",
            "attestations": "write",
        }


class TestReleasePermissions:
    def test_release_jobs_follow_least_privilege_split(self) -> None:
        """The signing job holds AWS creds (id-token) but must not hold
        contents:write; the GitHub-Release job holds contents:write but must
        not hold AWS creds. Keeping the two capabilities in separate jobs
        means a compromise of either job cannot both exfiltrate via AWS and
        tamper with the repo/release."""
        lines = _lines("release.yml")

        assert _workflow_permissions("release.yml") == {"contents": "read"}
        assert _permission_block(lines, "  build-wheel:") is None
        assert _permission_block(lines, "  build-desktop:") is None
        assert _permission_block(lines, "  release:") == {
            "contents": "read",
            "id-token": "write",
            "attestations": "write",
        }
        assert _permission_block(lines, "  github-release:") == {
            "contents": "write",
        }
