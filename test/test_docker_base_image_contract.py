"""Every image this repository builds starts from one pinned, canonical base.

Both recipes start from Amazon Linux 2023 as published by its own registry,
``public.ecr.aws/amazonlinux``, pinned by manifest-list digest. A ``FROM`` that
names any other registry (a Docker Hub short name such as ``python:3.12-slim``
included) or that drops the digest still builds, so nothing else notices it.

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

import pytest
from test_docker_wheel_layer_contract import _instructions

ROOT = Path(__file__).resolve().parents[1]

GATEWAY_DOCKERFILE = ROOT / "docker" / "Dockerfile"
CREW_DOCKERFILE = (
    ROOT
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "aws_control"
    / "crew"
    / "runtime"
    / "Dockerfile"
)

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


@pytest.mark.parametrize(
    "dockerfile", [GATEWAY_DOCKERFILE, CREW_DOCKERFILE], ids=["gateway", "crew-runtime"]
)
def test_every_stage_starts_from_the_pinned_al2023_base(dockerfile: Path) -> None:
    images = _from_images(dockerfile)
    assert images, f"{dockerfile} has no FROM"
    stray = [image for image in images if not _PINNED_BASE.fullmatch(image)]
    assert not stray, (
        f"{dockerfile.relative_to(ROOT)} starts a stage from {stray}: every stage must "
        "start from public.ecr.aws/amazonlinux/amazonlinux:2023 pinned by digest"
    )
    # One digest per file, so a bump cannot leave one stage behind.
    assert len(set(images)) == 1, images


#: Layers that are concatenated onto the crew runtime recipe (the MicroVM lane
#: builds base + crew + MicroVM as one Dockerfile), so they run on the same base.
STACKED_LAYERS = [
    CREW_DOCKERFILE.with_name("Dockerfile.crew"),
    CREW_DOCKERFILE.with_name("Dockerfile.microvm"),
]


@pytest.mark.parametrize(
    "dockerfile",
    [GATEWAY_DOCKERFILE, CREW_DOCKERFILE, *STACKED_LAYERS],
    ids=["gateway", "crew-runtime", "crew-layer", "microvm-layer"],
)
def test_no_layer_calls_a_debian_package_tool(dockerfile: Path) -> None:
    """AL2023 has no apt-get or dpkg, so a layer still calling one fails to build."""
    offenders = [
        inst
        for inst in _instructions(dockerfile)
        if inst.upper().startswith("RUN ") and re.search(r"\b(apt-get|apt|dpkg)\s", inst)
    ]
    assert not offenders, f"{dockerfile.relative_to(ROOT)}: {offenders[0][:160]!r}"


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
