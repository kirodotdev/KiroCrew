"""Tests for immutable stable-release promotion manifests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release_promotion.py"
SPEC = importlib.util.spec_from_file_location("release_promotion", SCRIPT)
assert SPEC and SPEC.loader
promotion = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(promotion)

SOURCE_SHA = "a" * 40
SOURCE_VERSION = "1.2.3-insider.4"
SOURCE_TAG = f"v{SOURCE_VERSION}"
BASE_VERSION = "1.2.3"
RUN_ID = 12345
IMAGE = "ghcr.io/kirodotdev/kirocrew"
IMAGE_DIGEST = f"sha256:{'b' * 64}"


def _bundle(path: Path) -> Path:
    path.mkdir()
    files = {
        "kirocrew-1.2.3rc4-py3-none-any.whl": b"wheel",
        "kirocrew-1.2.3rc4.tar.gz": b"sdist",
        "KiroCrew-x86_64.AppImage": b"appimage",
        "notarized.zip": b"mac-zip",
        "KiroCrew.dmg": b"dmg",
    }
    for name, body in files.items():
        (path / name).write_bytes(body)
    manifest = promotion.create_manifest(
        path,
        source_sha=SOURCE_SHA,
        source_tag=SOURCE_TAG,
        source_version=SOURCE_VERSION,
        base_version=BASE_VERSION,
        wheel_version="1.2.3rc4",
        source_run_id=RUN_ID,
        docker_image=IMAGE,
        docker_digest=IMAGE_DIGEST,
    )
    (path / promotion.MANIFEST_NAME).write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    return path


def test_manifest_round_trip_binds_source_and_every_shipping_file(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    manifest = promotion.verify_bundle(
        bundle,
        expected_source_sha=SOURCE_SHA,
        expected_base_version=BASE_VERSION,
        expected_source_run_id=RUN_ID,
        expected_source_tag=SOURCE_TAG,
        expected_docker_image=IMAGE,
    )

    assert set(manifest["artifacts"]) == {
        "wheel",
        "sdist",
        "appimage",
        "mac_zip",
        "dmg",
    }
    assert manifest["docker"]["digest"] == IMAGE_DIGEST
    for entry in manifest["artifacts"].values():
        assert len(entry["sha256"]) == 64
        assert entry["size"] > 0


def test_manifest_rejects_distribution_filename_from_other_wheel_version(
    tmp_path: Path,
) -> None:
    cases = (
        (
            "wheel",
            "kirocrew-1.2.3rc4-py3-none-any.whl",
            "kirocrew-9.9.9-py3-none-any.whl",
        ),
        ("sdist", "kirocrew-1.2.3rc4.tar.gz", "kirocrew-9.9.9.tar.gz"),
    )
    for logical_name, original_name, mismatched_name in cases:
        bundle = _bundle(tmp_path / logical_name)
        manifest_path = bundle / promotion.MANIFEST_NAME
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        (bundle / original_name).rename(bundle / mismatched_name)
        manifest["artifacts"][logical_name]["filename"] = mismatched_name
        manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")

        with pytest.raises(
            promotion.PromotionError,
            match=rf"{logical_name} filename .* does not match wheel_version",
        ):
            promotion.verify_bundle(bundle)


def test_tampered_candidate_file_fails_closed(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    (bundle / "KiroCrew.dmg").write_bytes(b"different bytes")

    with pytest.raises(promotion.PromotionError, match="does not match manifest"):
        promotion.verify_bundle(bundle)


def test_manifest_for_other_commit_or_release_line_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")

    with pytest.raises(promotion.PromotionError, match="source sha mismatch"):
        promotion.verify_bundle(bundle, expected_source_sha="c" * 40)
    with pytest.raises(promotion.PromotionError, match="base version mismatch"):
        promotion.verify_bundle(bundle, expected_base_version="1.2.4")


def test_candidate_selection_is_same_commit_same_base_and_newest_first() -> None:
    runs = [
        {
            "id": 2,
            "conclusion": "success",
            "head_sha": SOURCE_SHA,
            "head_branch": "v1.2.3-insider.2",
            "run_started_at": "2026-07-01T02:00:00Z",
        },
        {
            "id": 4,
            "conclusion": "failure",
            "head_sha": SOURCE_SHA,
            "head_branch": "v1.2.3-insider.4",
            "run_started_at": "2026-07-01T04:00:00Z",
        },
        {
            "id": 3,
            "conclusion": "success",
            "head_sha": SOURCE_SHA,
            "head_branch": "v1.2.3-insider.3",
            "run_started_at": "2026-07-01T03:00:00Z",
        },
        {
            "id": 99,
            "conclusion": "success",
            "head_sha": "d" * 40,
            "head_branch": "v1.2.3-insider.99",
            "run_started_at": "2026-07-01T05:00:00Z",
        },
        {
            "id": 100,
            "conclusion": "success",
            "head_sha": SOURCE_SHA,
            "head_branch": "v1.2.4-insider.1",
            "run_started_at": "2026-07-01T06:00:00Z",
        },
    ]

    selected = promotion.select_candidate_runs(
        runs, source_sha=SOURCE_SHA, base_version=BASE_VERSION
    )

    assert [run["id"] for run in selected] == [3, 2]


def _archive(bundle: Path, archive: Path, arcname_prefix: str = "") -> str:
    with zipfile.ZipFile(archive, "w") as output:
        for path in bundle.iterdir():
            output.write(path, f"{arcname_prefix}{path.name}")
    return f"sha256:{hashlib.sha256(archive.read_bytes()).hexdigest()}"


def _mock_resolver_api(
    monkeypatch: pytest.MonkeyPatch,
    archive: Path,
    artifact: dict[str, object],
) -> None:
    run = {
        "id": RUN_ID,
        "conclusion": "success",
        "head_sha": SOURCE_SHA,
        "head_branch": SOURCE_TAG,
        "run_started_at": "2026-07-01T03:00:00Z",
    }

    def fake_gh_json(endpoint: str) -> dict[str, object]:
        if "/actions/workflows/release.yml/runs?" in endpoint:
            return {"workflow_runs": [run]}
        if endpoint.endswith(f"/actions/runs/{RUN_ID}/artifacts?per_page=100"):
            return {"artifacts": [artifact]}
        raise AssertionError(f"unexpected GitHub API endpoint: {endpoint}")

    def fake_download(repository: str, artifact_id: int, destination: Path) -> None:
        assert repository == "kirodotdev/KiroCrew"
        assert artifact_id == 99
        destination.write_bytes(archive.read_bytes())

    monkeypatch.setattr(promotion, "_gh_json", fake_gh_json)
    monkeypatch.setattr(promotion, "_download_artifact", fake_download)


def _candidate_artifact(archive: Path, digest: object) -> dict[str, object]:
    artifact: dict[str, object] = {
        "id": 99,
        "name": f"stable-promotion-{BASE_VERSION}",
        "expired": False,
        "size_in_bytes": archive.stat().st_size,
    }
    if digest is not None:
        artifact["digest"] = digest
    return artifact


def test_matching_recorded_archive_digest_promotes_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path / "bundle")
    source_archive = tmp_path / "source.zip"
    digest = _archive(bundle, source_archive)
    artifact = _candidate_artifact(source_archive, digest)
    _mock_resolver_api(monkeypatch, source_archive, artifact)

    manifest, selected = promotion.resolve_candidate(
        repository="kirodotdev/KiroCrew",
        source_sha=SOURCE_SHA,
        base_version=BASE_VERSION,
        output_dir=tmp_path / "resolved",
        archive_path=tmp_path / "downloaded.zip",
    )

    assert manifest["source"]["workflow_run_id"] == RUN_ID
    assert manifest["docker"]["digest"] == IMAGE_DIGEST
    assert selected["digest"] == digest


def test_mismatched_recorded_archive_digest_aborts_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path / "bundle")
    source_archive = tmp_path / "source.zip"
    _archive(bundle, source_archive)
    artifact = _candidate_artifact(source_archive, f"sha256:{'0' * 64}")
    _mock_resolver_api(monkeypatch, source_archive, artifact)

    with pytest.raises(promotion.PromotionError, match="archive digest mismatch"):
        promotion.resolve_candidate(
            repository="kirodotdev/KiroCrew",
            source_sha=SOURCE_SHA,
            base_version=BASE_VERSION,
            output_dir=tmp_path / "resolved",
            archive_path=tmp_path / "downloaded.zip",
        )


def test_missing_recorded_archive_digest_aborts_promotion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path / "bundle")
    source_archive = tmp_path / "source.zip"
    _archive(bundle, source_archive)
    artifact = _candidate_artifact(source_archive, None)
    _mock_resolver_api(monkeypatch, source_archive, artifact)

    with pytest.raises(promotion.PromotionError, match="artifact.digest must be"):
        promotion.resolve_candidate(
            repository="kirodotdev/KiroCrew",
            source_sha=SOURCE_SHA,
            base_version=BASE_VERSION,
            output_dir=tmp_path / "resolved",
            archive_path=tmp_path / "downloaded.zip",
        )


def test_archive_digest_is_verified_before_safe_extraction(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    archive = tmp_path / "candidate.zip"
    digest = _archive(bundle, archive)
    output = tmp_path / "resolved"

    promotion.extract_verified_archive(archive, output, expected_digest=digest)
    promotion.verify_bundle(output, expected_source_sha=SOURCE_SHA)

    with pytest.raises(promotion.PromotionError, match="archive digest mismatch"):
        promotion.extract_verified_archive(
            archive, tmp_path / "other", expected_digest=f"sha256:{'0' * 64}"
        )


def test_archive_path_traversal_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    archive = tmp_path / "candidate.zip"
    digest = _archive(bundle, archive, arcname_prefix="../")

    with pytest.raises(promotion.PromotionError, match="unsafe path"):
        promotion.extract_verified_archive(archive, tmp_path / "resolved", expected_digest=digest)
