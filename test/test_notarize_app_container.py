"""The macOS app is notarized inside a disk image, never as the signed zip.

The universal app holds more than 65,535 files, so the zip the signing service
returns is Zip64, and notarytool refuses a Zip64 archive before it uploads
anything. The notarize step must hand notarize.sh a .dmg built from the
unpacked app, and the staple step must staple that same unpacked app.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "sign-and-notarize.yml"


def _step(name: str) -> str:
    steps = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["notarize"]["steps"]
    matches = [s["run"] for s in steps if s.get("name") == name]
    assert len(matches) == 1, f"expected one {name!r} step"
    return matches[0]


def test_app_notarization_submits_a_disk_image_not_the_signed_zip() -> None:
    run = _step("Notarize with Apple notary service")
    submits = [line.strip() for line in run.splitlines() if "notarize.sh" in line]
    assert submits == ["bash packaging/signing/notarize.sh work/notarize-app.dmg"]
    assert "hdiutil create" in run and "-srcfolder" in run
    assert "work/stapled" in run


def test_staple_uses_the_app_the_notarize_step_unpacked() -> None:
    run = _step("Staple and verify")
    assert "cd work/stapled" in run
    assert "unzip" not in run
