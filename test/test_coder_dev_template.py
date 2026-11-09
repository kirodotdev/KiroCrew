"""The kirocrew-dev Coder template's toolchain must match the repository's own.

``docs/guides/assets/coder-kirocrew-dev`` ships a Coder workspace image for
developing this repository from a Session Environments chat. Its Python, Node
and Semgrep versions are copied from what the repository declares, and nothing
fails when one side moves without the other: the image still builds, and a
remote session only finds out when ``make build`` refuses its interpreter or a
rule test runs on a different Semgrep. These checks read only text, so they
need no Docker daemon.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "docs" / "guides" / "assets" / "coder-kirocrew-dev"
DOCKERFILE = TEMPLATE / "build" / "Dockerfile"
SEMGREP_LOCK = TEMPLATE / "build" / "semgrep-requirements.txt"

_FROM = re.compile(r"^FROM\s+(\S+)", re.MULTILINE)
_PINNED_IMAGE = re.compile(r"^(?P<name>[\w./-]+):(?P<tag>[\w.-]+)@sha256:[0-9a-f]{64}$")


def _from_images() -> dict[str, str]:
    """``image name -> tag`` for every FROM line, each required to be digest-pinned."""
    images: dict[str, str] = {}
    for ref in _FROM.findall(DOCKERFILE.read_text(encoding="utf-8")):
        match = _PINNED_IMAGE.match(ref)
        assert match, f"{ref} must be pinned as name:tag@sha256:<digest>"
        images[match["name"]] = match["tag"]
    return images


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def test_python_matches_requires_python_floor() -> None:
    requires = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "requires-python"
    ]
    floor = re.fullmatch(r">=\s*(\d+\.\d+)", requires)
    assert floor, f"unexpected requires-python {requires!r}; teach this test the new form"
    tag = _from_images()["python"]
    assert tag.startswith(
        f"{floor[1]}-"
    ), f"python:{tag} is not the requires-python floor {floor[1]}"


def test_node_matches_nvmrc_and_engines() -> None:
    tag = _from_images()["node"]
    node = re.match(r"(\d+\.\d+\.\d+)-", tag)
    assert node, f"node:{tag} must name an exact release"
    nvmrc = (ROOT / ".nvmrc").read_text(encoding="utf-8").strip()
    assert node[1].split(".")[0] == nvmrc, f"node:{tag} is not .nvmrc's Node {nvmrc}"
    engines = re.search(
        r'"node":\s*">=\s*([\d.]+)"',
        (ROOT / "website" / "package.json").read_text(encoding="utf-8"),
    )
    assert engines, "website/package.json engines.node changed form; teach this test the new form"
    assert _version(node[1]) >= _version(engines[1])


def test_semgrep_matches_ci() -> None:
    workflow = (ROOT / ".github" / "workflows" / "code-review.yml").read_text(encoding="utf-8")
    ci = re.findall(r"\bsemgrep==([\d.]+)", workflow)
    assert len(set(ci)) == 1, f"expected one CI semgrep pin, found {ci}"
    lock = re.findall(r"^semgrep==([\d.]+)", SEMGREP_LOCK.read_text(encoding="utf-8"), re.MULTILINE)
    assert lock == ci[:1], f"template semgrep {lock} differs from CI {ci[0]}"


def test_image_runs_as_non_root_and_never_pipes_to_a_shell() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    users = re.findall(r"^USER\s+(\S+)", text, re.MULTILINE)
    assert users and users[-1] not in {"root", "0"}
    for path in (DOCKERFILE, TEMPLATE / "main.tf"):
        body = "\n".join(
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )
        assert not re.search(r"\b(?:curl|wget)\b[^\n]*\|\s*(?:ba|z)?sh\b", body), path
