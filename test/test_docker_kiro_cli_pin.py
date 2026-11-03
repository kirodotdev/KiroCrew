"""The Docker image installs the kiro-cli release the desktop build pins.

``packaging/kiro-cli-version`` names the release and ``packaging/kiro-cli-sha256``
holds each artifact's sha256 keyed ``<version>/<file>``. The image reads both, so a
rebuild of a tag is reproducible, and never the mutable upstream manifest.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
_DOCKERFILE = _ROOT / "docker" / "Dockerfile"
_DOCKERIGNORE = _ROOT / ".dockerignore"
_PUBLISH = _ROOT / ".github" / "workflows" / "publish-docker.yml"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_dockerfile_never_reads_the_upstream_manifest() -> None:
    assert _text(_DOCKERFILE).count("manifest.json") == 0


def test_the_install_step_bind_mounts_the_pin_files() -> None:
    text = _text(_DOCKERFILE)
    assert "--mount=type=bind,source=packaging,target=/tmp/kiro-cli-pin" in text
    assert "/tmp/kiro-cli-pin/kiro-cli-version" in text
    assert "/tmp/kiro-cli-pin/kiro-cli-sha256" in text
    assert "sha256sum -c -" in text
    # Never copied into a layer.
    assert not re.search(r"^\s*(COPY|ADD)\b.*packaging", text, re.M)


def test_the_build_context_admits_exactly_the_two_pin_files() -> None:
    lines = [ln.strip() for ln in _text(_DOCKERIGNORE).splitlines()]
    assert "!packaging/kiro-cli-version" in lines
    assert "!packaging/kiro-cli-sha256" in lines
    assert "packaging/*" in lines, "the rest of packaging/ stays out of the context"


def test_the_pin_has_a_linux_zip_line_for_every_image_arch() -> None:
    version = _text(_ROOT / "packaging" / "kiro-cli-version").strip()
    shas = _text(_ROOT / "packaging" / "kiro-cli-sha256").splitlines()
    for arch in ("x86_64", "aarch64"):
        want = f"{version}/kirocli-{arch}-linux.zip"
        hits = [ln for ln in shas if ln.split("  ", 1)[-1] == want]
        assert len(hits) == 1, f"no single sha line for {want}"
        assert re.fullmatch(r"[0-9a-f]{64}", hits[0].split()[0])


def test_the_publish_lane_labels_the_image_with_the_pin() -> None:
    doc = yaml.safe_load(_text(_PUBLISH))
    steps = [s for job in doc["jobs"].values() for s in job.get("steps", [])]
    (resolve,) = [s for s in steps if s.get("id") == "kirocli"]
    assert "packaging/kiro-cli-version" in resolve["run"]
    assert "manifest.json" not in resolve["run"]
