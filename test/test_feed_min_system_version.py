"""The macOS update feed's OS floor is read from the app ZIP and written as a Darwin version."""

from __future__ import annotations

import importlib.util
import plistlib
import zipfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "feed_min_system_version.py"
_spec = importlib.util.spec_from_file_location("feed_min_system_version", _SCRIPT)
assert _spec and _spec.loader
floor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(floor)


def _app_zip(tmp_path: Path, plist: dict | None, *, fmt=plistlib.FMT_XML, name="Kiro Crew") -> Path:
    path = tmp_path / "app.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}.app/Contents/MacOS/{name}", b"\0")
        if plist is not None:
            archive.writestr(f"{name}.app/Contents/Info.plist", plistlib.dumps(plist, fmt=fmt))
    return path


@pytest.mark.parametrize(
    ("marketing", "darwin"),
    [
        ("12.0", "21.0.0"),
        ("13.0", "22.0.0"),
        ("14.0", "23.0.0"),
        ("11.0", "20.0.0"),
        ("10.15", "19.0.0"),
        ("10.13", "17.0.0"),
        ("13.0.0", "22.0.0"),
        ("15.0", "24.0.0"),
        ("26.0", "25.0.0"),
        ("27.0", "26.0.0"),
    ],
)
def test_marketing_versions_map_to_the_kernel_os_release_reports(marketing, darwin) -> None:
    assert floor.darwin_version(marketing) == darwin


@pytest.mark.parametrize(
    "marketing", ["13.3", "12.0.1", "10.15.7", "9.0", "16.0", "25.0", "26.1", "thirteen", ""]
)
def test_a_floor_with_no_exact_kernel_version_is_refused(marketing) -> None:
    # Rounding 13.3 down to Darwin 22.0.0 would let macOS 13.0-13.2 update into a
    # build that refuses to launch there.
    with pytest.raises(floor.FeedFloorError):
        floor.darwin_version(marketing)


@pytest.mark.parametrize("fmt", [plistlib.FMT_XML, plistlib.FMT_BINARY])
def test_the_floor_is_read_from_the_apps_info_plist(tmp_path, fmt, capsys) -> None:
    path = _app_zip(
        tmp_path, {"CFBundleName": "Kiro Crew", "LSMinimumSystemVersion": "13.0"}, fmt=fmt
    )
    assert floor.main(["x", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "22.0.0"


def test_a_nightly_bundle_name_is_read_the_same(tmp_path, capsys) -> None:
    path = _app_zip(tmp_path, {"LSMinimumSystemVersion": "12.0"}, name="KiroCrew Nightly")
    assert floor.main(["x", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "21.0.0"


@pytest.mark.parametrize("plist", [None, {}, {"LSMinimumSystemVersion": ""}])
def test_an_artifact_without_a_floor_fails_closed(tmp_path, plist, capsys) -> None:
    path = _app_zip(tmp_path, plist)
    assert floor.main(["x", str(path)]) == 1
    assert "feed_min_system_version:" in capsys.readouterr().err


def test_a_zip_that_is_not_an_archive_fails_closed(tmp_path) -> None:
    path = tmp_path / "app.zip"
    path.write_bytes(b"not a zip")
    assert floor.main(["x", str(path)]) == 1
