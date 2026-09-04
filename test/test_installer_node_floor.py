"""The installer must not build the frontend against an unsupported Node.

Amazon Linux 2023 ships node 18. ``install.sh`` gated its "already installed"
branch on ``has node`` alone, so that system node matched, EVERY install branch
(apt/dnf/brew and nvm) was skipped, and the frontend build then ran under an
unsupported interpreter -- emitting only ``EBADENGINE`` warnings and no usable
``dist/``. The launch still reported success (the Python gateway answers
``/api/health`` without a frontend), so the first symptom a user saw was
"Dashboard HTML not found" (#3220).

The floor is now consulted at DETECTION time via ``node_supported()``. These
tests extract that helper from the real ``install.sh`` and run it against fake
``node`` binaries, rather than executing the whole installer (which would
install packages).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="POSIX shell installer; not exercised on native Windows"
)


def _helper_source() -> str:
    """The floor constant plus `has`/`node_supported`, lifted verbatim.

    Extracted rather than duplicated so the test cannot drift from the shipped
    definition -- a copy would keep passing after the real helper regressed.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    floor = next(
        line for line in text.splitlines() if line.startswith("NODE_MIN_MAJOR=")
    )
    has_start = text.index("has() {")
    fn_start = text.index("node_supported() {")
    fn_end = text.index("\n}", fn_start) + len("\n}")
    return f"{floor}\n{text[has_start:text.index(chr(10), has_start)]}\n{text[fn_start:fn_end]}\n"


def _run_with_node(tmp_path: Path, version: str | None) -> int:
    """Return `node_supported`'s exit status with a fake `node` reporting *version*.

    ``version=None`` means no node on PATH at all.
    """
    binbox = tmp_path / "bin"
    binbox.mkdir(exist_ok=True)
    if version is not None:
        node = binbox / "node"
        node.write_text(f'#!/bin/sh\n[ "$1" = "--version" ] && echo "{version}"\n')
        node.chmod(0o755)
    # Isolated PATH: only the fake node plus the shell utilities the helper
    # itself calls, so a host node can never satisfy the check.
    tools = binbox / "tools"
    tools.mkdir(exist_ok=True)
    for util in ("sed", "cut", "sh", "env"):
        real = shutil.which(util)
        if real:
            link = tools / util
            if not link.exists():
                link.symlink_to(real)
    script = _helper_source() + "node_supported\n"
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover - POSIX hosts always have bash
        pytest.skip("bash not available")
    # Absolute bash: the isolated PATH below deliberately excludes it, and
    # execvp would otherwise resolve argv[0] against that same stripped PATH.
    return subprocess.run(
        [bash, "-c", script],
        env={"PATH": f"{binbox}:{tools}"},
        capture_output=True,
        text=True,
        timeout=30,
    ).returncode


class TestNodeFloorIsAuthoritative:
    @pytest.mark.parametrize("version", ["v18.20.8", "v20.11.0", "v16.0.0"])
    def test_an_under_floor_node_is_refused(self, tmp_path: Path, version: str) -> None:
        """The AL2023 case: node exists, so the old `has node` gate accepted it
        and skipped every install branch."""
        assert _run_with_node(tmp_path, version) != 0, (
            f"{version} was accepted; the installer would build against it"
        )

    @pytest.mark.parametrize("version", ["v22.0.0", "v24.5.1", "v25.0.0"])
    def test_a_supported_node_is_accepted(self, tmp_path: Path, version: str) -> None:
        """Equally important: a usable node must NOT trigger a reinstall."""
        assert _run_with_node(tmp_path, version) == 0, f"{version} was rejected"

    def test_no_node_at_all_is_refused(self, tmp_path: Path) -> None:
        assert _run_with_node(tmp_path, None) != 0

    def test_a_broken_node_binary_is_refused_not_crashed_on(
        self, tmp_path: Path
    ) -> None:
        """A node that errors instead of printing a version reports v0 and
        fails the comparison -- it must not abort the installer."""
        binbox = tmp_path / "bin"
        binbox.mkdir()
        node = binbox / "node"
        node.write_text('#!/bin/sh\necho "loader error" >&2\nexit 1\n')
        node.chmod(0o755)
        script = _helper_source() + "node_supported\n"
        bash = shutil.which("bash")
        out = subprocess.run(
            [bash or "/bin/bash", "-c", script],
            env={"PATH": str(binbox) + ":" + os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert out.returncode != 0, "a broken node binary was treated as supported"


class TestFrontendBuildHeadroom:
    def test_the_build_raises_the_v8_heap_ceiling(self) -> None:
        """V8's ~2 GB default is not enough for this bundle; the OOM shows up
        as a build that dies with no clear cause and no dist/ (#3220)."""
        text = INSTALL_SH.read_text(encoding="utf-8")
        assert "--max-old-space-size" in text
        # Set only as a default so an operator can still override it.
        assert 'NODE_OPTIONS="${NODE_OPTIONS:-' in text

    def test_the_floor_is_defined_once(self) -> None:
        """The constant is consulted by both detection and the post-install
        check; two definitions could disagree."""
        text = INSTALL_SH.read_text(encoding="utf-8")
        assert text.count("NODE_MIN_MAJOR=") == 1
