"""Fail-closed contract tests for macOS assets on GitHub Releases.

The release job has ``contents: write`` and is the final trust-boundary hop
before files become public.  These tests execute its actual shell step so an
unsigned fallback, a broad ``find | head`` selector, or a superficial presence
check cannot silently reappear.
"""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="the GitHub Release assembly step runs under bash on ubuntu-latest",
)

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
STEP_NAME = "Assemble release assets (require gated macOS artifacts)"
CHANNEL = "stable"
VERSION = "1.2.3"
ARTIFACT_NAME = f"KiroCrew-notarized-{CHANNEL}-{VERSION}"


def _assembly_script() -> str:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["github-release"]["steps"]
    step = next((item for item in steps if item.get("name") == STEP_NAME), None)
    assert step is not None, f"release workflow step {STEP_NAME!r} not found"

    script = step["run"]
    script = script.replace("${{ needs.version.outputs.channel }}", CHANNEL)
    script = script.replace("${{ needs.version.outputs.version }}", VERSION)
    assert "${{" not in script, "test harness left an unresolved GitHub expression"
    return script


def _artifact_dir(root: Path, name: str = ARTIFACT_NAME) -> Path:
    path = root / "artifacts" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_valid_zip(path: Path, app_name: str = "KiroCrew.app") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{app_name}/Contents/Info.plist", "<plist/>")


def _write_valid_dmg(path: Path) -> None:
    # hdiutil's UDIF output ends in a 512-byte trailer beginning with "koly".
    path.write_bytes(b"test payload" + b"koly" + bytes(508))


def _write_valid_handoff(root: Path, name: str = ARTIFACT_NAME) -> Path:
    artifact = _artifact_dir(root, name)
    _write_valid_zip(artifact / "notarized.zip")
    _write_valid_dmg(artifact / "KiroCrew.dmg")
    return artifact


def _run_assembly(root: Path) -> subprocess.CompletedProcess[str]:
    (root / "artifacts").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", "-c", _assembly_script()],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_exact_gated_artifact_does_not_fall_back_to_unsigned(tmp_path: Path) -> None:
    """A valid-looking unsigned build or stale notarized run cannot be selected."""
    unsigned = _artifact_dir(tmp_path, "unsigned-build-darwin-universal")
    _write_valid_zip(unsigned / "KiroCrew-universal-mac.zip")
    _write_valid_dmg(unsigned / "KiroCrew.dmg")
    _write_valid_handoff(tmp_path, "KiroCrew-notarized-stable-1.2.2")

    result = _run_assembly(tmp_path)

    assert result.returncode != 0
    assert "Required gated macOS ZIP is missing or empty" in result.stderr + result.stdout
    assert not list((tmp_path / "release").glob("*mac.zip"))
    assert not list((tmp_path / "release").glob("*.dmg"))


@pytest.mark.parametrize(
    ("missing_name", "expected_error"),
    (
        ("notarized.zip", "Required gated macOS ZIP is missing or empty"),
        ("KiroCrew.dmg", "Required gated macOS DMG is missing or empty"),
    ),
)
def test_incomplete_gated_handoff_fails(
    tmp_path: Path, missing_name: str, expected_error: str
) -> None:
    artifact = _write_valid_handoff(tmp_path)
    (artifact / missing_name).unlink()

    result = _run_assembly(tmp_path)

    assert result.returncode != 0
    assert expected_error in result.stderr + result.stdout


def test_corrupt_notarized_zip_fails(tmp_path: Path) -> None:
    artifact = _artifact_dir(tmp_path)
    (artifact / "notarized.zip").write_bytes(b"not a zip")
    _write_valid_dmg(artifact / "KiroCrew.dmg")

    result = _run_assembly(tmp_path)

    assert result.returncode != 0
    assert "is not a valid ZIP archive" in result.stderr + result.stdout


def test_non_udif_dmg_fails(tmp_path: Path) -> None:
    artifact = _artifact_dir(tmp_path)
    _write_valid_zip(artifact / "notarized.zip")
    (artifact / "KiroCrew.dmg").write_bytes(b"not a UDIF image")

    result = _run_assembly(tmp_path)

    assert result.returncode != 0
    assert "is not a valid UDIF DMG" in result.stderr + result.stdout


def test_exact_gated_handoff_is_renamed_for_the_release(tmp_path: Path) -> None:
    gated = _write_valid_handoff(tmp_path)
    unsigned = _artifact_dir(tmp_path, "unsigned-build-darwin-universal")
    (unsigned / "unsigned-mac.zip").write_bytes(b"unsigned zip")
    (unsigned / "unsigned.dmg").write_bytes(b"unsigned dmg")
    (tmp_path / "artifacts" / "cli.whl").write_bytes(b"wheel")

    result = _run_assembly(tmp_path)

    assert result.returncode == 0, result.stderr + result.stdout
    release = tmp_path / "release"
    release_zip = release / f"KiroCrew-{VERSION}-universal-mac.zip"
    release_dmg = release / f"KiroCrew-{VERSION}-universal.dmg"
    assert release_zip.read_bytes() == (gated / "notarized.zip").read_bytes()
    assert release_dmg.read_bytes() == (gated / "KiroCrew.dmg").read_bytes()
    assert not (release / "unsigned-mac.zip").exists()
    assert not (release / "unsigned.dmg").exists()
