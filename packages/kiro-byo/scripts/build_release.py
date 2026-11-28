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

try:
    from scripts.release_file_policy import unsafe_release_path
except ModuleNotFoundError:  # direct `python scripts/build_release.py` invocation
    from release_file_policy import unsafe_release_path

ROOT = Path(__file__).resolve().parents[1]

RELEASE_ROOT_FILES = {
    "app.json",
    "BUILD_INFO.json",
    "VERSION",
    "README.md",
    "configure.ps1",
    "manage.ps1",
}
RELEASE_DIRS = {"assets", "backend", "config", "integration", "src", "ui"}
SOURCE_EXCLUDED_PARTS = {
    ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__",
    "build", "dist", ".venv", "venv",
}
SOURCE_EXCLUDED_SUFFIXES = {".pyc", ".pyo"}
SOURCE_EXCLUDED_NAMES = {".coverage"}


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
        # A source-tree symlink can silently include a file outside the project
        # when ZipInfo/from_file or read_bytes follows it. Refuse the build.
        if path.is_symlink():
            raise RuntimeError(f"release tree contains a symbolic link: {path.relative_to(ROOT)}")
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in SOURCE_EXCLUDED_PARTS or part.endswith(".egg-info") for part in rel.parts):
            continue
        if (path.name in SOURCE_EXCLUDED_NAMES or path.name.startswith(".coverage.")
                or unsafe_release_path(rel) is not None):
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
    # Each build carries its SemVer product identity plus a deterministic
    # code fingerprint to each archive without changing Crew's semver App ID.
    # BUILD_INFO is intentionally excluded from the runtime fingerprint.
    files = _relative_files()
    runtime_sha = _runtime_digest(files)
    build_info = {
        "product": "Kiro BYO",
        "version": version,
        "build_id": f"{version}+{runtime_sha[:16]}",
        "runtime_sha256": runtime_sha,
        "candidate_label": f"{version}-host-approval-upgrade-candidate",
        "crew_target": "KiroCrew 0.12.x; guarded legacy integration remains; not native-certified",
        "crew_commit": "User-supplied Crew 0.12.0 source was audited; origin not cryptographically verified",
        "crew_candidate": "Generic external ACP Harness upstream extension is separately under review; not installed by this App",
        "crew_candidate_commit": "not merged upstream",
        "crew_candidate_source_contract": "BYO session host_tool_approval=required is available; Crew production enforcement not certified",
        "crew_012_status": "Legacy 20-file Crew integration retained; real desktop upgrade and permission E2E not verified",
        "windows_e2e": "not_verified",
        "cross_platform_e2e": {
            "windows": "not_verified", "macos": "not_verified", "linux": "BYO ACP child tested; Crew Gateway not verified",
        },
        "widget_intent": "natural-language + context-aware followups + always-on Crew hint; Crew UI E2E not verified",
        "dsh_phase5_status": "BYO Work Ledger mapping exists; native Crew dispatch integration not installed",
        "crew_host_task_id_injection": "not_wired_into_official_Crew_dispatch",
        "crew_work_ledger_preflight": "Crew Work Ledger production dispatch E2E not verified",
        "headless_ui_timing": "no visible overlay; measurement in page memory only",
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
