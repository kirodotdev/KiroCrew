#!/usr/bin/env python3
"""Offline, read-only-to-user Crew-source + Dashboard archive integration audit.

Reconstructs a temporary ``kiro_crew/static/dist`` from the separately supplied
Dashboard archive, runs the *real* BYO fail-closed source preflight, and can
exercise install/reinstall/uninstall transactions only in the temporary tree.
It never installs BYO into the user's Crew or certifies a real Gateway runtime.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from kiro_byo import integration_manager as manager  # noqa: E402

SOURCE_PREFIX = "KiroCrew-0.12.0/src/kiro_crew/"
DASHBOARD_PREFIX = "dist/"
MAX_FILES = 15000
MAX_TOTAL_UNPACKED = 512 * 1024 * 1024
MAX_ENTRY = 32 * 1024 * 1024


class ArchiveError(ValueError):
    """An archive is unsafe, incorrectly laid out, or ambiguous."""


def _archive_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_target(member: ZipInfo, prefix: str, dest: Path) -> tuple[Path, bool] | None:
    # On Windows, archive creators commonly store backslash-separated directories.
    # Do not let the OS's path flavor decide whether '..' is recognized.
    raw = member.filename.replace("\\", "/")
    if not raw.startswith(prefix):
        return None
    if "\x00" in raw or raw.startswith("/") or any(part in ("", ".", "..") for part in raw.rstrip("/").split("/")):
        raise ArchiveError(f"unsafe archive member: {member.filename!r}")
    parts = PurePosixPath(raw).parts
    if any(":" in part for part in parts):
        raise ArchiveError(f"invalid path component: {member.filename!r}")
    # Unix zip permission bits can identify symlinks. Reject them to avoid a
    # dashboard archive creating indirections out of the isolated directory.
    mode = member.external_attr >> 16
    if stat.S_IFMT(mode) == stat.S_IFLNK:
        raise ArchiveError(f"archive symlink refused: {member.filename!r}")
    relative = raw[len(prefix):].rstrip("/")
    if not relative:
        return None
    return dest.joinpath(*relative.split("/")), raw.endswith("/")


def extract_owned_tree(archive: Path, prefix: str, dest: Path) -> dict[str, int]:
    """Copy only the expected archive root, with traversal/alias/size guards."""
    seen: set[str] = set()
    count = total = 0
    with ZipFile(archive) as opened:
        if len(opened.infolist()) > MAX_FILES:
            raise ArchiveError("archive has too many members")
        for item in opened.infolist():
            mapped = _safe_target(item, prefix, dest)
            if mapped is None:
                continue
            destination, is_dir = mapped
            # Case folding matters because production Crew can run on Windows.
            relative_key = destination.relative_to(dest).as_posix().casefold()
            if relative_key in seen:
                raise ArchiveError(f"archive contains duplicate/Windows-alias path: {item.filename!r}")
            seen.add(relative_key)
            count += 1
            if is_dir:
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if item.file_size > MAX_ENTRY:
                raise ArchiveError(f"archive member exceeds size limit: {item.filename!r}")
            total += item.file_size
            if total > MAX_TOTAL_UNPACKED:
                raise ArchiveError("archive total extracted size exceeds limit")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with opened.open(item) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output, 1024 * 1024)
    if not count:
        raise ArchiveError(f"no members found under expected prefix {prefix!r}")
    return {"entries": count, "unpackedBytes": total}


class _ResourceLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for key, value in attrs:
            if key in {"src", "href"} and value and value.startswith(("/", "./")):
                target = urlsplit(value).path
                if target and not target.startswith("//"):
                    self.paths.append(unquote(target).lstrip("./"))


def missing_local_resources(dist: Path) -> list[str]:
    parser = _ResourceLinks()
    parser.feed((dist / "index.html").read_text(encoding="utf-8"))
    missing = set()
    for raw in parser.paths:
        rel = PurePosixPath(raw)
        if ".." in rel.parts or rel.is_absolute():
            missing.add("invalid:" + raw)
        elif not (dist / Path(*rel.parts)).is_file():
            missing.add("/" + raw)
    return sorted(missing)


def transaction_round_trip(crew_package: Path, version: str, temp: Path) -> dict:
    """Real install functions, fake running-Crew locator, isolated filesystem."""
    adapter = ROOT / "integration/kiro_crew/v0.12-plus"
    compat = json.loads((adapter / "compatibility.json").read_text(encoding="utf-8"))
    touched = [p.removeprefix("src/kiro_crew/") for p in compat["generated_files"]]
    touched.append("static/dist/index.html")
    original = {p: hashlib.sha256((crew_package / p).read_bytes()).hexdigest() for p in touched}
    created = [p.removeprefix("src/kiro_crew/") for p in compat["created_files"]]
    site = crew_package.parent
    home = temp / "byo-app-data"
    with patch.object(manager, "running_crew", lambda: (version, crew_package, site)):
        installed = manager.install_from_app(ROOT, home, version=(ROOT / "VERSION").read_text(encoding="utf8").strip())
        if not installed.get("ok"):
            raise AssertionError("isolated install did not succeed")
        if not all((crew_package / p).is_file() for p in created):
            raise AssertionError("installed created-file Harness is missing")
        installed_hash = hashlib.sha256((crew_package / "static/dist/index.html").read_bytes()).hexdigest()
        repeated = manager.install_from_app(ROOT, home, version=(ROOT / "VERSION").read_text(encoding="utf8").strip())
        if not repeated.get("ok") or installed_hash != hashlib.sha256((crew_package / "static/dist/index.html").read_bytes()).hexdigest():
            raise AssertionError("repeated integration changed dashboard bytes")
        uninstalled = manager.uninstall_from_app(ROOT, home)
    restored = all(hashlib.sha256((crew_package / p).read_bytes()).hexdigest() == value for p, value in original.items())
    cleared = all(not (crew_package / p).exists() for p in created)
    if not restored or not cleared:
        raise AssertionError("uninstall did not restore every pristine source and remove BYO-created files")
    return {
        "status": "pass", "install": installed.get("ok"),
        "reinstall": repeated.get("ok"), "uninstall": uninstalled.get("ok"),
        "restoredOriginalFiles": len(original), "removedCreatedFiles": len(created),
        "dashboardAssetsPatched": installed.get("dashboard_assets", []),
        "verifiedLayer": "isolated filesystem transaction; not a running Crew Gateway",
    }


def audit(source: Path, dashboard: Path, version: str, *, exercise_transaction: bool = True) -> dict:
    result: dict = {
        "sourceArchiveSha256": _archive_sha256(source),
        "dashboardArchiveSha256": _archive_sha256(dashboard),
        "crewVersionClaim": version,
        "archivesProvenSameOfficialBuild": False,
        "realCrewDesktopE2E": "not_verified",
        "workerMcpPermissionsE2E": "not_verified",
    }
    with tempfile.TemporaryDirectory(prefix="kiro-byo-combined-dashboard-") as tmp:
        root = Path(tmp)
        crew_package = root / "crew/src/kiro_crew"
        result["sourceExtraction"] = extract_owned_tree(source, SOURCE_PREFIX, crew_package)
        result["dashboardExtraction"] = extract_owned_tree(dashboard, DASHBOARD_PREFIX, crew_package / "static/dist")
        dist = crew_package / "static/dist"
        result["missingIndexReferences"] = missing_local_resources(dist)
        adapter = ROOT / "integration/kiro_crew/v0.12-plus"
        ok, issues, _manifest, compat = manager._adapter_matches(adapter, version, crew_package)
        result["sourceAndDashboardPreflight"] = "pass" if ok else "refused"
        result["activeCrewTransformFiles"] = len(compat.get("generated_files") or [])
        result["preflightIssues"] = issues
        if ok and exercise_transaction:
            result["isolatedInstallRoundTrip"] = transaction_round_trip(crew_package, version, root)
        else:
            result["isolatedInstallRoundTrip"] = {"status": "not_run"}
        result["status"] = "pass_with_static_asset_warning" if ok and result["missingIndexReferences"] else ("pass" if ok else "refused")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--crew-source-zip", type=Path, required=True)
    parser.add_argument("--dashboard-zip", type=Path, required=True)
    parser.add_argument("--crew-version", default="0.12.0")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    try:
        report = audit(args.crew_source_zip, args.dashboard_zip, args.crew_version,
                       exercise_transaction=not args.preflight_only)
        code = 0 if report["sourceAndDashboardPreflight"] == "pass" else 2
    except (ArchiveError, OSError, ValueError, AssertionError, manager.IntegrationError) as exc:
        report = {"status": "refused", "error": str(exc), "realCrewDesktopE2E": "not_verified"}
        code = 2
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
