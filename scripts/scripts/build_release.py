#!/usr/bin/env python3
"""Build Kiro BYO end-user and source archives from one audited tree.

The release archive intentionally contains only files needed to install/run the
KiroCrew App. The source archive contains the complete development tree needed
for audit, tests and further modification. Mutable user state, caches and build
artifacts are excluded from both.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RELEASE_ROOT_FILES = {
    "app.json",
    "BUILD_INFO.json",
    "VERSION",
    "README.md",
    "CHANGELOG.md",
    "WINDOWS_QUICKSTART.md",
    "QUICKSTART.md",
    "configure.ps1",
    "manage.ps1",
}
RELEASE_DIRS = {"assets", "backend", "config", "integration", "src", "ui"}
SOURCE_EXCLUDED_PARTS = {
    ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__",
    "build", "dist", ".venv", "venv",
}
SOURCE_EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".bak", ".backup", ".orig", ".rej"}
SOURCE_EXCLUDED_NAMES = {".coverage"}


def _is_editor_artifact(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith("~") or any(
        marker in name for marker in (".bak-", ".backup-", ".orig-", ".rej-")
    )
MUTABLE_PATHS = {
    "config/providers.json",
    "config/native_tools.json",
    "config/acp_agents.json",
}
MUTABLE_PREFIXES = (
    "data/sessions/",
    "data/telemetry/",
    "data/runtime_trace/",
    "data/agent_runtime/",
    "data/runs/",
    "data/agent_workspaces/",
    "data/audit/",
)
RUNTIME_COMPARE_DIRS = {"assets", "backend", "config", "integration", "src", "ui"}
RUNTIME_COMPARE_ROOT_FILES = {"app.json", "VERSION", "configure.ps1", "manage.ps1"}


def _version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _relative_files() -> list[Path]:
    out: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in SOURCE_EXCLUDED_PARTS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if path.name in SOURCE_EXCLUDED_NAMES or _is_editor_artifact(path):
            continue
        if path.suffix.lower() in SOURCE_EXCLUDED_SUFFIXES:
            continue
        posix = rel.as_posix()
        if posix in MUTABLE_PATHS or any(posix.startswith(prefix) for prefix in MUTABLE_PREFIXES):
            continue
        out.append(rel)
    return sorted(out, key=lambda value: value.as_posix())


def _release_files(files: list[Path]) -> list[Path]:
    selected: list[Path] = []
    for rel in files:
        if len(rel.parts) == 1 and rel.name in RELEASE_ROOT_FILES:
            selected.append(rel)
        elif rel.parts and rel.parts[0] in RELEASE_DIRS:
            selected.append(rel)
    return selected


def _write_zip(path: Path, files: list[Path]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # Explicit root directory makes Explorer extraction/inspection tidy.
        root_info = zipfile.ZipInfo("Kiro BYO/")
        root_info.external_attr = 0o40755 << 16
        zf.writestr(root_info, b"")
        for rel in files:
            source = ROOT / rel
            arcname = f"Kiro BYO/{rel.as_posix()}"
            info = zipfile.ZipInfo.from_file(source, arcname=arcname)
            # Keep builds reproducible enough for checksums across machines by
            # normalising metadata that is not part of product identity.
            info.create_system = 3
            info.external_attr = (0o100644 & 0xFFFF) << 16
            with source.open("rb") as fh:
                zf.writestr(info, fh.read(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def _runtime_digest(files: list[Path]) -> str:
    digest = hashlib.sha256()
    for rel in files:
        if not (
            (len(rel.parts) == 1 and rel.name in RUNTIME_COMPARE_ROOT_FILES)
            or (rel.parts and rel.parts[0] in RUNTIME_COMPARE_DIRS)
        ):
            continue
        digest.update(rel.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((ROOT / rel).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _build_wheel(output_dir: Path) -> Path:
    wheel_dir = output_dir / "wheel"
    if wheel_dir.exists():
        shutil.rmtree(wheel_dir)
    wheel_dir.mkdir(parents=True)
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", str(wheel_dir)],
        cwd=ROOT,
        check=True,
    )
    wheels = sorted(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected exactly one wheel, found {len(wheels)}")
    final = output_dir / wheels[0].name
    shutil.copy2(wheels[0], final)
    shutil.rmtree(wheel_dir)
    return final


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--skip-wheel", action="store_true")
    args = parser.parse_args()

    subprocess.run([sys.executable, str(ROOT / "scripts" / "release_check.py")], cwd=ROOT, check=True)

    version = _version()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    # Separate builds may retain product version 0.2.3. Attach a deterministic
    # code fingerprint to each archive without changing Crew's semver App ID.
    # BUILD_INFO is intentionally excluded from the runtime fingerprint.
    files = _relative_files()
    runtime_sha = _runtime_digest(files)
    build_info = {
        "product": "Kiro BYO",
        "version": version,
        "build_id": f"{version}+{runtime_sha[:16]}",
        "runtime_sha256": runtime_sha,
        "crew_target": "v0.11.0-rc.1 source-audited; guarded 0.11.0-rc.2+, 0.11.0, 0.11.1-rc.1+, 0.11.1 (local contract checks only)",
        "crew_commit": "uploaded_rc1_archive_source_checked",
        "crew_candidate": "v0.11.0-rc.2+, v0.11.0, v0.11.1-rc.1+ and v0.11.1 (provisional strict source-contract preflight)",
        "crew_candidate_commit": "v0.11.1-rc.2_tag_a27d362_observed_upstream_not_fully_source_audited",
        "crew_candidate_source_contract": "0111_rc2_strict_local_source_and_dashboard_preflight_23_seams_no_cross_platform_e2e",
        "windows_e2e": "not_verified",
        "cross_platform_e2e": {
            "windows": "not_verified",
            "macos": "not_verified",
            "linux": "not_verified",
        },
        "candidate_label": "0.3.0-v17-user-sssp-byo-icon-only-crew-0111rc2-preflight-e2e-pending",
    }
    (ROOT / "BUILD_INFO.json").write_text(
        json.dumps(build_info, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    files = _relative_files()
    release_files = _release_files(files)
    if not release_files:
        raise RuntimeError("release file set is empty")

    release_zip = output_dir / f"Kiro_BYO_{version}-release.zip"
    source_zip = output_dir / f"Kiro_BYO_{version}-source.zip"
    _write_zip(release_zip, release_files)
    _write_zip(source_zip, files)

    # Release is a strict subset of source for all runtime-bearing files.
    if _runtime_digest(release_files) != _runtime_digest(files):
        raise RuntimeError("release/source runtime fingerprint mismatch")

    artifacts = [release_zip, source_zip]
    if not args.skip_wheel:
        artifacts.append(_build_wheel(output_dir))

    sums = output_dir / "SHA256SUMS.txt"
    sums.write_text(
        "".join(f"{_sha256(path)}  {path.name}\n" for path in artifacts),
        encoding="utf-8",
    )
    print(f"release files: {len(release_files)} / source files: {len(files)}")
    print(f"runtime fingerprint: {_runtime_digest(files)}")
    for path in artifacts:
        print(path)
    print(sums)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
