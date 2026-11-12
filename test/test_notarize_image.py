"""Drills for ``packaging/signing/notarize-image.sh`` and its place in the workflow.

The universal app's signed zip was refused by ``notarytool`` ("must be a zip
archive", exit 64, before anything was uploaded) while the single-arch zips and
the earlier universal zip were accepted. The workflow then sends the app as a
disk image instead. These drills stand in a fake ``hdiutil`` that records what it
was asked to image; the real tools run only on the macOS runner.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "signing" / "notarize-image.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "sign-and-notarize.yml"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None or shutil.which("unzip") is None,
    reason="notarize-image.sh is a Bash script that needs unzip",
)

# Records the folder it was given and what is in it, then writes the image.
FAKE_HDIUTIL = r"""#!/usr/bin/env bash
[ "$1" = create ] || exit 64
echo "$*" > "$FAKE_DIR/hdiutil-args"
find "$3" -type f | wc -l > "$FAKE_DIR/hdiutil-files"
find "$3" -type d -name '*.app' > "$FAKE_DIR/hdiutil-apps"
for last; do :; done
echo image > "$last"
"""


def _zip(path: Path, entries: int) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for i in range(entries):
            archive.writestr(f"App.app/Contents/f{i}", "")
    return path


def _run(
    tmp_path: Path, archive: Path, hdiutil: str = FAKE_HDIUTIL, python3: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the script the way the workflow does: from a directory, with ``signed.zip``
    and ``work`` given as relative paths (``archive`` lives in ``tmp_path``)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("hdiutil", hdiutil), ("python3", python3)):
        if body is not None:
            tool = bin_dir / name
            tool.write_text(body, encoding="utf-8")
            tool.chmod(0o755)
    (tmp_path / "work").mkdir()
    return subprocess.run(
        ["bash", str(SCRIPT), archive.name, "work"],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_DIR": str(tmp_path),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,  # flake-ok: bounds a hung child process, not a test boundary
    )


def test_the_app_is_imaged_whole_and_the_unpacked_copy_is_removed(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "signed.zip", 5)
    proc = _run(tmp_path, archive)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "work/notarize-input.dmg\n"
    assert (tmp_path / "work" / "notarize-input.dmg").read_text(encoding="utf-8") == "image\n"
    assert (tmp_path / "hdiutil-files").read_text(encoding="utf-8").strip() == "5"
    assert (tmp_path / "hdiutil-apps").read_text(encoding="utf-8").strip().endswith("App.app")
    assert "-format UDZO" in (tmp_path / "hdiutil-args").read_text(encoding="utf-8")
    assert [p.name for p in (tmp_path / "work").iterdir()] == ["notarize-input.dmg"]


def test_a_universal_sized_zip_goes_into_the_image_whole(tmp_path: Path) -> None:
    # 94,000 entries is the universal app: over what a classic zip end record counts.
    archive = _zip(tmp_path / "signed.zip", 94000)
    proc = _run(tmp_path, archive)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "hdiutil-files").read_text(encoding="utf-8").strip() == "94000"
    assert "94000 entries read by python" in proc.stderr
    # Over 65,535 the end record carries 65535 and a pointer to the zip64 record.
    assert "end record entries=65535" in proc.stderr
    assert "zip64_locator=True" in proc.stderr


def test_the_log_says_what_the_zip_is(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "signed.zip", 3)
    proc = _run(tmp_path, archive)
    assert proc.returncode == 0, proc.stderr
    assert "first bytes 504b0304" in proc.stderr
    assert "end record entries=3" in proc.stderr
    assert "zip64_locator=False" in proc.stderr


def test_a_log_that_cannot_be_written_does_not_stop_the_unpack(tmp_path: Path) -> None:
    # The inspection only logs; if it dies, `unzip` still gets its turn.
    archive = _zip(tmp_path / "signed.zip", 4)
    proc = _run(tmp_path, archive, python3="#!/usr/bin/env bash\nexit 1\n")
    assert proc.returncode == 0, proc.stderr
    assert "could not be inspected" in proc.stderr
    assert proc.stdout == "work/notarize-input.dmg\n"
    assert (tmp_path / "hdiutil-files").read_text(encoding="utf-8").strip() == "4"


def test_a_failing_image_build_fails_the_script(tmp_path: Path) -> None:
    archive = _zip(tmp_path / "signed.zip", 4)
    proc = _run(tmp_path, archive, hdiutil="#!/usr/bin/env bash\nexit 1\n")
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert [p.name for p in (tmp_path / "work").iterdir()] == []


def test_a_file_that_is_not_a_zip_fails_and_the_log_still_says_what_it_is(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "signed.zip"
    archive.write_bytes(b"not a zip")
    proc = _run(tmp_path, archive)
    assert proc.returncode != 0
    assert proc.stdout == ""
    assert "9 bytes, first bytes 6e6f7420" in proc.stderr
    assert "no end-of-central-directory record" in proc.stderr


def test_the_workflow_falls_back_to_the_image_only_when_the_zip_is_refused() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = {s.get("name"): s for s in workflow["jobs"]["notarize"]["steps"]}
    run = steps["Notarize with Apple notary service"]["run"]
    # The zip goes first, unchanged; the image only on notarytool's exit 64.
    assert "if bash packaging/signing/notarize.sh work/signed.zip; then" in run
    assert '[ "$rc" -eq 64 ] || exit "$rc"' in run
    assert "notarize-image.sh work/signed.zip work/notarize-scratch)" in run
    assert run.index("notarize.sh work/signed.zip") < run.index("notarize-image.sh")
    assert 'bash packaging/signing/notarize.sh "$IMAGE"' in run
    # The artifact steps pick up every work/*.dmg, so the image lives one level down
    # and is removed once Apple has accepted it.
    assert run.index('notarize.sh "$IMAGE"') < run.index("rm -rf work/notarize-scratch")
    uploads = [s for s in steps.values() if "work/*.dmg" in str(s.get("with", ""))]
    assert uploads, "the artifact steps no longer take work/*.dmg"
    # The staple step still unpacks the signed zip itself, so the ticket is stapled
    # to the same app whichever file Apple was sent.
    assert "unzip -q ../signed.zip" in steps["Staple and verify"]["run"]
