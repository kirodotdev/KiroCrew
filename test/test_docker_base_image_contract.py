"""The gateway image starts from one pinned, canonical base.

``docker/Dockerfile`` starts every stage from Amazon Linux 2023 as published by
its own registry, ``public.ecr.aws/amazonlinux``, pinned by manifest-list digest.
A ``FROM`` that names any other registry (a Docker Hub short name such as
``python:3.12-slim`` included) or that drops the digest still builds, so nothing
else notices it. The aws-control app's crew images are owned by that app's own
repository and are not checked here.

tini has no AL2023 package, so the gateway image builds it from a pinned source
archive in a throwaway stage. The entrypoint execs it after the credential
scrub, so an image without it fails at start; the pin is checked here so the
source cannot change without a reviewed sha256 change.

Static and offline: it reads only the Dockerfile text, through the same
instruction reader ``test_docker_wheel_layer_contract.py`` uses.
"""

from __future__ import annotations

import re
from pathlib import Path

from test_docker_wheel_layer_contract import _instructions

ROOT = Path(__file__).resolve().parents[1]

GATEWAY_DOCKERFILE = ROOT / "docker" / "Dockerfile"

_PINNED_BASE = re.compile(r"public\.ecr\.aws/amazonlinux/amazonlinux:2023@sha256:[0-9a-f]{64}")


def _from_images(dockerfile: Path) -> list[str]:
    """The image operand of every ``FROM``, skipping ``--platform``-style flags."""
    images = []
    for inst in _instructions(dockerfile):
        if not inst.upper().startswith("FROM "):
            continue
        operands = [t for t in inst.split()[1:] if not t.startswith("--")]
        images.append(operands[0])
    return images


def test_every_stage_starts_from_the_pinned_al2023_base() -> None:
    images = _from_images(GATEWAY_DOCKERFILE)
    assert images, f"{GATEWAY_DOCKERFILE} has no FROM"
    stray = [image for image in images if not _PINNED_BASE.fullmatch(image)]
    assert not stray, (
        f"docker/Dockerfile starts a stage from {stray}: every stage must "
        "start from public.ecr.aws/amazonlinux/amazonlinux:2023 pinned by digest"
    )
    # One digest per file, so a bump cannot leave one stage behind.
    assert len(set(images)) == 1, images


def test_no_layer_calls_a_debian_package_tool() -> None:
    """AL2023 has no apt-get or dpkg, so a layer still calling one fails to build."""
    offenders = [
        inst
        for inst in _instructions(GATEWAY_DOCKERFILE)
        if inst.upper().startswith("RUN ") and re.search(r"\b(apt-get|apt|dpkg)\s", inst)
    ]
    assert not offenders, f"docker/Dockerfile still calls a Debian package tool: {offenders}"


def test_tini_is_built_from_a_verified_pinned_source() -> None:
    text = GATEWAY_DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ARG\s+TINI_VERSION=\d+\.\d+\.\d+\s*$", text, re.MULTILINE)
    assert re.search(r"^ARG\s+TINI_SRC_SHA256=[0-9a-f]{64}\s*$", text, re.MULTILINE)

    (build,) = [inst for inst in _instructions(GATEWAY_DOCKERFILE) if "tini.tar.gz" in inst]
    checked = build.index('echo "${TINI_SRC_SHA256}  /tmp/tini.tar.gz" | sha256sum -c -')
    assert checked < build.index(
        "tar -xzf /tmp/tini.tar.gz"
    ), "the archive is unpacked before it is verified"

    copies = [
        inst
        for inst in _instructions(GATEWAY_DOCKERFILE)
        if inst.startswith("COPY --from=tini-builder /usr/local/bin/tini /usr/local/bin/tini")
    ]
    assert copies, "the final stage never copies tini in, so the entrypoint's exec fails"


def test_the_wheel_is_importable_by_isolated_mode_python() -> None:
    """AL2023 drops /usr/local site-packages under ``python -I``, which the KAS step uses."""
    (install,) = [inst for inst in _instructions(GATEWAY_DOCKERFILE) if "/tmp/wheels/*.whl" in inst]
    assert "pip install --no-cache-dir --prefix /usr /tmp/wheels/*.whl" in install
    assert "python -I -c 'import kiro_crew'" in install
