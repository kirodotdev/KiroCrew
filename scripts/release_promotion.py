#!/usr/bin/env python3
"""Create and verify immutable release-candidate promotion manifests.

Stable releases use this tool to resolve a successful prerelease workflow run
for the same commit, verify the GitHub artifact archive by its API-recorded
SHA-256 digest, safely extract it, and verify every shipping file against the
candidate manifest.  No source build occurs on the stable path.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlencode

SCHEMA_VERSION = 1
MANIFEST_NAME = "promotion-manifest.json"
ARTIFACT_NAMES = {
    "wheel": re.compile(r"^kirocrew-[A-Za-z0-9_.]+-py3-none-any\.whl$"),
    "sdist": re.compile(r"^kirocrew-[A-Za-z0-9_.]+\.tar\.gz$"),
    "appimage": re.compile(r"^KiroCrew-x86_64\.AppImage$"),
    "mac_zip": re.compile(r"^notarized\.zip$"),
    "dmg": re.compile(r"^KiroCrew\.dmg$"),
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
BASE_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
MAX_ARCHIVE_BYTES = 5 * 1024 * 1024 * 1024
MAX_EXTRACTED_BYTES = 8 * 1024 * 1024 * 1024
MAX_FILE_COUNT = len(ARTIFACT_NAMES) + 1


class PromotionError(ValueError):
    """The candidate cannot be promoted without violating byte identity."""


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha512_base64(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PromotionError(f"{field} must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise PromotionError(f"{field} must be one line")
    return value


def _artifact_paths(bundle_dir: Path) -> dict[str, Path]:
    files = [path for path in bundle_dir.iterdir() if path.is_file()]
    result: dict[str, Path] = {}
    for logical_name, pattern in ARTIFACT_NAMES.items():
        matches = [path for path in files if pattern.fullmatch(path.name)]
        if len(matches) != 1:
            names = sorted(path.name for path in matches)
            raise PromotionError(f"expected exactly one {logical_name} artifact, found {names}")
        result[logical_name] = matches[0]
    return result


def _require_wheel_version_filename(
    logical_name: str, filename: str, wheel_version: str
) -> None:
    prefix = f"kirocrew-{wheel_version}"
    if logical_name == "wheel":
        valid = filename.startswith(f"{prefix}-")
    elif logical_name == "sdist":
        valid = filename == f"{prefix}.tar.gz"
    else:
        return
    if not valid:
        raise PromotionError(
            f"{logical_name} filename {filename!r} does not match "
            f"wheel_version {wheel_version!r}"
        )


def create_manifest(
    bundle_dir: Path,
    *,
    source_sha: str,
    source_tag: str,
    source_version: str,
    base_version: str,
    wheel_version: str,
    source_run_id: int,
    docker_image: str,
    docker_digest: str,
) -> dict[str, Any]:
    """Build a manifest over the canonical files in *bundle_dir*."""
    _validate_source_identity(
        source_sha=source_sha,
        source_tag=source_tag,
        source_version=source_version,
        base_version=base_version,
        source_run_id=source_run_id,
    )
    if not DIGEST_RE.fullmatch(docker_digest):
        raise PromotionError("docker digest must be sha256:<64 lowercase hex>")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/kirocrew", docker_image):
        raise PromotionError("docker image must be the canonical lowercase GHCR image")
    if not re.fullmatch(r"[A-Za-z0-9_.]+", wheel_version):
        raise PromotionError("wheel version contains unsupported characters")

    paths = _artifact_paths(bundle_dir)
    allowed = {path.name for path in paths.values()}
    extras = {
        path.name
        for path in bundle_dir.iterdir()
        if path.is_file() and path.name not in allowed and path.name != MANIFEST_NAME
    }
    if extras:
        raise PromotionError(f"unexpected files in promotion bundle: {sorted(extras)}")

    artifacts: dict[str, dict[str, Any]] = {}
    for logical_name, path in paths.items():
        artifacts[logical_name] = {
            "filename": path.name,
            "sha256": _hash_file(path, "sha256"),
            "sha512": _sha512_base64(path),
            "size": path.stat().st_size,
        }

    return {
        "schema": SCHEMA_VERSION,
        "source": {
            "sha": source_sha,
            "tag": source_tag,
            "version": source_version,
            "base_version": base_version,
            "wheel_version": wheel_version,
            "workflow_run_id": source_run_id,
        },
        "artifacts": artifacts,
        "docker": {"image": docker_image, "digest": docker_digest},
    }


def _validate_source_identity(
    *,
    source_sha: str,
    source_tag: str,
    source_version: str,
    base_version: str,
    source_run_id: int,
) -> None:
    if not SHA_RE.fullmatch(source_sha):
        raise PromotionError("source sha must be 40 lowercase hex characters")
    if not BASE_VERSION_RE.fullmatch(base_version):
        raise PromotionError("base version must be a bare three-part semver")
    if not source_version.startswith(f"{base_version}-"):
        raise PromotionError("source version must be a prerelease of the stable base")
    if source_tag != f"v{source_version}":
        raise PromotionError("source tag must exactly identify the source version")
    if not isinstance(source_run_id, int) or source_run_id <= 0:
        raise PromotionError("source workflow run id must be positive")


def validate_manifest(
    manifest: Mapping[str, Any],
    bundle_dir: Path,
    *,
    expected_source_sha: str | None = None,
    expected_base_version: str | None = None,
    expected_source_run_id: int | None = None,
    expected_source_tag: str | None = None,
    expected_docker_image: str | None = None,
) -> dict[str, Any]:
    """Validate manifest shape, identity, and every file digest."""
    if set(manifest) != {"schema", "source", "artifacts", "docker"}:
        raise PromotionError("promotion manifest has unexpected top-level fields")
    if manifest["schema"] != SCHEMA_VERSION:
        raise PromotionError(f"unsupported promotion manifest schema {manifest['schema']!r}")

    source = manifest["source"]
    if not isinstance(source, dict) or set(source) != {
        "sha",
        "tag",
        "version",
        "base_version",
        "wheel_version",
        "workflow_run_id",
    }:
        raise PromotionError("promotion manifest source block has unexpected fields")
    source_sha = _require_string(source["sha"], "source.sha")
    source_tag = _require_string(source["tag"], "source.tag")
    source_version = _require_string(source["version"], "source.version")
    base_version = _require_string(source["base_version"], "source.base_version")
    wheel_version = _require_string(source["wheel_version"], "source.wheel_version")
    source_run_id = source["workflow_run_id"]
    _validate_source_identity(
        source_sha=source_sha,
        source_tag=source_tag,
        source_version=source_version,
        base_version=base_version,
        source_run_id=source_run_id,
    )
    if not re.fullmatch(r"[A-Za-z0-9_.]+", wheel_version):
        raise PromotionError("source wheel version contains unsupported characters")

    expected = {
        "source sha": (source_sha, expected_source_sha),
        "base version": (base_version, expected_base_version),
        "source run id": (source_run_id, expected_source_run_id),
        "source tag": (source_tag, expected_source_tag),
    }
    for label, (actual, wanted) in expected.items():
        if wanted is not None and actual != wanted:
            raise PromotionError(f"{label} mismatch: expected {wanted!r}, got {actual!r}")

    artifacts = manifest["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != set(ARTIFACT_NAMES):
        raise PromotionError("promotion manifest must list exactly the shipping artifacts")
    expected_filenames: set[str] = set()
    for logical_name, pattern in ARTIFACT_NAMES.items():
        entry = artifacts[logical_name]
        if not isinstance(entry, dict) or set(entry) != {
            "filename",
            "sha256",
            "sha512",
            "size",
        }:
            raise PromotionError(f"artifact {logical_name} has unexpected fields")
        filename = _require_string(entry["filename"], f"artifacts.{logical_name}.filename")
        if Path(filename).name != filename or not pattern.fullmatch(filename):
            raise PromotionError(f"unsafe or invalid {logical_name} filename {filename!r}")
        _require_wheel_version_filename(logical_name, filename, wheel_version)
        if filename in expected_filenames:
            raise PromotionError(f"duplicate artifact filename {filename!r}")
        expected_filenames.add(filename)
        sha256 = _require_string(entry["sha256"], f"artifacts.{logical_name}.sha256")
        sha512 = _require_string(entry["sha512"], f"artifacts.{logical_name}.sha512")
        size = entry["size"]
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise PromotionError(f"artifact {logical_name} has an invalid sha256")
        try:
            decoded_sha512 = base64.b64decode(sha512, validate=True)
        except ValueError as exc:
            raise PromotionError(f"artifact {logical_name} has an invalid sha512") from exc
        if len(decoded_sha512) != hashlib.sha512().digest_size:
            raise PromotionError(f"artifact {logical_name} has an invalid sha512 length")
        if not isinstance(size, int) or size < 0 or size > MAX_EXTRACTED_BYTES:
            raise PromotionError(f"artifact {logical_name} has an invalid size")
        path = bundle_dir / filename
        if not path.is_file() or path.is_symlink():
            raise PromotionError(f"artifact {logical_name} is missing or not a regular file")
        if path.stat().st_size != size:
            raise PromotionError(f"artifact {logical_name} size does not match manifest")
        if _hash_file(path, "sha256") != sha256:
            raise PromotionError(f"artifact {logical_name} sha256 does not match manifest")
        if _sha512_base64(path) != sha512:
            raise PromotionError(f"artifact {logical_name} sha512 does not match manifest")

    actual_files = {path.name for path in bundle_dir.iterdir() if path.is_file()}
    if actual_files != expected_filenames | {MANIFEST_NAME}:
        raise PromotionError(
            "promotion bundle file set differs from manifest: "
            f"expected {sorted(expected_filenames | {MANIFEST_NAME})}, "
            f"got {sorted(actual_files)}"
        )

    docker = manifest["docker"]
    if not isinstance(docker, dict) or set(docker) != {"image", "digest"}:
        raise PromotionError("promotion manifest docker block has unexpected fields")
    image = _require_string(docker["image"], "docker.image")
    digest = _require_string(docker["digest"], "docker.digest")
    if not re.fullmatch(r"ghcr\.io/[a-z0-9_.-]+/kirocrew", image):
        raise PromotionError("promotion manifest has a non-canonical docker image")
    if expected_docker_image is not None and image != expected_docker_image:
        raise PromotionError(
            f"docker image mismatch: expected {expected_docker_image!r}, got {image!r}"
        )
    if not DIGEST_RE.fullmatch(digest):
        raise PromotionError("promotion manifest has an invalid docker digest")

    return dict(manifest)


def verify_bundle(bundle_dir: Path, **expected: Any) -> dict[str, Any]:
    manifest_path = bundle_dir / MANIFEST_NAME
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise PromotionError(f"{MANIFEST_NAME} is missing or not a regular file")
    if manifest_path.stat().st_size > 1024 * 1024:
        raise PromotionError("promotion manifest exceeds the 1 MiB bound")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"cannot read promotion manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PromotionError("promotion manifest root must be an object")
    return validate_manifest(manifest, bundle_dir, **expected)


def select_candidate_runs(
    runs: Iterable[Mapping[str, Any]], *, source_sha: str, base_version: str
) -> list[Mapping[str, Any]]:
    """Newest-first successful prerelease runs for this exact commit/base."""
    prefix = f"v{base_version}-"
    candidates = [
        run
        for run in runs
        if run.get("conclusion") == "success"
        and run.get("head_sha") == source_sha
        and isinstance(run.get("head_branch"), str)
        and run["head_branch"].startswith(prefix)
    ]
    return sorted(
        candidates,
        key=lambda run: (str(run.get("run_started_at") or ""), int(run.get("id") or 0)),
        reverse=True,
    )


def extract_verified_archive(archive_path: Path, output_dir: Path, *, expected_digest: str) -> None:
    """Digest-check and safely extract the bounded GitHub artifact archive."""
    if not DIGEST_RE.fullmatch(expected_digest):
        raise PromotionError("GitHub artifact API did not return a valid sha256 digest")
    if not archive_path.is_file() or archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PromotionError("promotion archive is missing or exceeds the size bound")
    actual_digest = f"sha256:{_hash_file(archive_path, 'sha256')}"
    if actual_digest != expected_digest:
        raise PromotionError(
            f"promotion archive digest mismatch: expected {expected_digest}, got {actual_digest}"
        )

    try:
        archive = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise PromotionError("promotion artifact is not a valid zip archive") from exc
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = [info.filename for info in infos]
        if len(infos) != MAX_FILE_COUNT or len(set(names)) != len(names):
            raise PromotionError("promotion archive has an unexpected or duplicate file set")
        if sum(info.file_size for info in infos) > MAX_EXTRACTED_BYTES:
            raise PromotionError("promotion archive expands beyond the size bound")
        expected_names = {MANIFEST_NAME} | {
            pattern.pattern.removeprefix("^").removesuffix("$")
            for logical_name, pattern in ARTIFACT_NAMES.items()
            if logical_name in {"appimage", "mac_zip", "dmg"}
        }
        for info in infos:
            name = info.filename
            if Path(name).name != name or name.startswith(("/", "\\")):
                raise PromotionError(f"unsafe path in promotion archive: {name!r}")
            mode = (info.external_attr >> 16) & 0o170000
            if mode and not stat.S_ISREG(mode):
                raise PromotionError(f"non-regular entry in promotion archive: {name!r}")
        # Wheel and sdist names are versioned, so validate the full set through
        # the same patterns used by the manifest rather than hard-coding them.
        for logical_name, pattern in ARTIFACT_NAMES.items():
            matches = [name for name in names if pattern.fullmatch(name)]
            if len(matches) != 1:
                raise PromotionError(
                    f"promotion archive must contain exactly one {logical_name} file"
                )
        if MANIFEST_NAME not in names:
            raise PromotionError(f"promotion archive is missing {MANIFEST_NAME}")
        del expected_names  # documents fixed names without weakening pattern checks

        output_dir.mkdir(parents=True, exist_ok=False)
        for info in infos:
            destination = output_dir / info.filename
            with archive.open(info) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)


def _gh_json(endpoint: str) -> dict[str, Any]:
    result = subprocess.run(
        ["gh", "api", endpoint],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise PromotionError(f"GitHub API request failed for {endpoint}: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PromotionError(f"GitHub API returned invalid JSON for {endpoint}") from exc
    if not isinstance(payload, dict):
        raise PromotionError(f"GitHub API returned a non-object for {endpoint}")
    return payload


def _download_artifact(repository: str, artifact_id: int, destination: Path) -> None:
    with destination.open("wb") as output:
        result = subprocess.run(
            ["gh", "api", f"repos/{repository}/actions/artifacts/{artifact_id}/zip"],
            check=False,
            stdout=output,
            stderr=subprocess.PIPE,
            timeout=600,
        )
    if result.returncode != 0:
        destination.unlink(missing_ok=True)
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        raise PromotionError(f"GitHub artifact download failed: {stderr}")


def resolve_candidate(
    *,
    repository: str,
    source_sha: str,
    base_version: str,
    output_dir: Path,
    archive_path: Path,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Resolve and verify the newest promotable candidate for the commit."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise PromotionError("repository must be an owner/name identifier")
    if not SHA_RE.fullmatch(source_sha):
        raise PromotionError("source sha must be 40 lowercase hex characters")
    if not BASE_VERSION_RE.fullmatch(base_version):
        raise PromotionError("base version must be a bare three-part semver")

    query = urlencode(
        {
            "event": "push",
            "status": "success",
            "head_sha": source_sha,
            "per_page": "100",
        }
    )
    runs_payload = _gh_json(f"repos/{repository}/actions/workflows/release.yml/runs?{query}")
    runs = runs_payload.get("workflow_runs")
    if not isinstance(runs, list):
        raise PromotionError("GitHub workflow-runs response omitted workflow_runs")
    candidates = select_candidate_runs(runs, source_sha=source_sha, base_version=base_version)
    if not candidates:
        raise PromotionError(
            "no successful prerelease workflow run exists for this exact commit and base"
        )

    artifact_name = f"stable-promotion-{base_version}"
    selected_run: Mapping[str, Any] | None = None
    selected_artifact: Mapping[str, Any] | None = None
    for run in candidates:
        run_id = int(run.get("id") or 0)
        artifacts_payload = _gh_json(
            f"repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100"
        )
        artifacts = artifacts_payload.get("artifacts")
        if not isinstance(artifacts, list):
            raise PromotionError(f"artifact response for run {run_id} omitted artifacts")
        matches = [
            artifact
            for artifact in artifacts
            if artifact.get("name") == artifact_name and artifact.get("expired") is False
        ]
        if len(matches) > 1:
            raise PromotionError(f"run {run_id} has duplicate {artifact_name} artifacts")
        if matches:
            selected_run = run
            selected_artifact = matches[0]
            break
    if selected_run is None or selected_artifact is None:
        raise PromotionError(
            f"successful prerelease runs exist, but none has an unexpired {artifact_name} artifact"
        )

    artifact_id = int(selected_artifact.get("id") or 0)
    artifact_size = int(selected_artifact.get("size_in_bytes") or 0)
    artifact_digest = _require_string(selected_artifact.get("digest"), "artifact.digest")
    if artifact_id <= 0 or artifact_size <= 0 or artifact_size > MAX_ARCHIVE_BYTES:
        raise PromotionError("promotion artifact metadata has an invalid id or size")

    _download_artifact(repository, artifact_id, archive_path)
    extract_verified_archive(archive_path, output_dir, expected_digest=artifact_digest)
    source_run_id = int(selected_run["id"])
    source_tag = _require_string(selected_run.get("head_branch"), "workflow_run.head_branch")
    docker_image = f"ghcr.io/{repository.split('/', 1)[0].lower()}/kirocrew"
    manifest = verify_bundle(
        output_dir,
        expected_source_sha=source_sha,
        expected_base_version=base_version,
        expected_source_run_id=source_run_id,
        expected_source_tag=source_tag,
        expected_docker_image=docker_image,
    )
    return manifest, selected_artifact


def _write_outputs(path: Path, values: Mapping[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for key, raw_value in values.items():
            value = str(raw_value)
            if not re.fullmatch(r"[A-Za-z0-9_./:@+-]+", value):
                raise PromotionError(f"unsafe GitHub output value for {key}")
            stream.write(f"{key}={value}\n")


def _create_command(args: argparse.Namespace) -> None:
    bundle_dir = Path(args.bundle_dir)
    manifest = create_manifest(
        bundle_dir,
        source_sha=args.source_sha,
        source_tag=args.source_tag,
        source_version=args.source_version,
        base_version=args.base_version,
        wheel_version=args.wheel_version,
        source_run_id=args.source_run_id,
        docker_image=args.docker_image,
        docker_digest=args.docker_digest,
    )
    output = bundle_dir / MANIFEST_NAME
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    verify_bundle(bundle_dir)
    print(f"wrote {output}")


def _verify_command(args: argparse.Namespace) -> None:
    manifest = verify_bundle(
        Path(args.bundle_dir),
        expected_source_sha=args.expected_source_sha,
        expected_base_version=args.expected_base_version,
        expected_source_run_id=args.expected_source_run_id,
        expected_source_tag=args.expected_source_tag,
        expected_docker_image=args.expected_docker_image,
    )
    if args.github_output:
        source = manifest["source"]
        docker = manifest["docker"]
        _write_outputs(
            Path(args.github_output),
            {
                "source_version": source["version"],
                "source_tag": source["tag"],
                "source_run_id": source["workflow_run_id"],
                "docker_image": docker["image"],
                "docker_digest": docker["digest"],
            },
        )
    print("promotion bundle verified")


def _resolve_command(args: argparse.Namespace) -> None:
    manifest, artifact = resolve_candidate(
        repository=args.repository,
        source_sha=args.source_sha,
        base_version=args.base_version,
        output_dir=Path(args.output_dir),
        archive_path=Path(args.archive_path),
    )
    source = manifest["source"]
    docker = manifest["docker"]
    _write_outputs(
        Path(args.github_output),
        {
            "source_version": source["version"],
            "source_tag": source["tag"],
            "source_run_id": source["workflow_run_id"],
            "docker_image": docker["image"],
            "docker_digest": docker["digest"],
            "artifact_id": artifact["id"],
            "artifact_digest": artifact["digest"],
        },
    )
    print(
        f"resolved {source['tag']} run {source['workflow_run_id']} "
        f"artifact {artifact['id']} ({artifact['digest']})"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create")
    create.add_argument("--bundle-dir", required=True)
    create.add_argument("--source-sha", required=True)
    create.add_argument("--source-tag", required=True)
    create.add_argument("--source-version", required=True)
    create.add_argument("--base-version", required=True)
    create.add_argument("--wheel-version", required=True)
    create.add_argument("--source-run-id", required=True, type=int)
    create.add_argument("--docker-image", required=True)
    create.add_argument("--docker-digest", required=True)
    create.set_defaults(handler=_create_command)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--bundle-dir", required=True)
    verify.add_argument("--expected-source-sha")
    verify.add_argument("--expected-base-version")
    verify.add_argument("--expected-source-run-id", type=int)
    verify.add_argument("--expected-source-tag")
    verify.add_argument("--expected-docker-image")
    verify.add_argument("--github-output")
    verify.set_defaults(handler=_verify_command)

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--repository", required=True)
    resolve.add_argument("--source-sha", required=True)
    resolve.add_argument("--base-version", required=True)
    resolve.add_argument("--output-dir", required=True)
    resolve.add_argument("--archive-path", required=True)
    resolve.add_argument("--github-output", required=True)
    resolve.set_defaults(handler=_resolve_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except (OSError, PromotionError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
