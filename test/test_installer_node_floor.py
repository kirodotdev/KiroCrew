"""The installer must not build the frontend against an unsupported Node.

Amazon Linux 2023 ships node 18. ``install.sh`` gated its "already installed"
branch on ``has node`` alone, so that system node matched, EVERY install branch
(apt/dnf/brew and nvm) was skipped, and the frontend build then ran under an
unsupported interpreter -- emitting only ``EBADENGINE`` warnings and no usable
``dist/``. The launch still reported success (the Python gateway answers
``/api/health`` without a frontend), so the first symptom a user saw was
"Dashboard HTML not found".

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
    floor = next(line for line in text.splitlines() if line.startswith("NODE_MIN_VERSION="))
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
        encoding="utf-8",
        timeout=30,
    ).returncode


class TestNodeFloorIsAuthoritative:
    @pytest.mark.parametrize(
        "version", ["v18.20.8", "v20.11.0", "v16.0.0", "v22.0.0", "v22.9.0", "v22.11.1"]
    )
    def test_an_under_floor_node_is_refused(self, tmp_path: Path, version: str) -> None:
        """The AL2023 case: node exists, so the old `has node` gate accepted it
        and skipped every install branch."""
        assert (
            _run_with_node(tmp_path, version) != 0
        ), f"{version} was accepted; the installer would build against it"

    @pytest.mark.parametrize("version", ["v22.12.0", "v22.20.1", "v24.5.1", "v25.0.0"])
    def test_a_supported_node_is_accepted(self, tmp_path: Path, version: str) -> None:
        """Equally important: a usable node must NOT trigger a reinstall."""
        assert _run_with_node(tmp_path, version) == 0, f"{version} was rejected"

    def test_no_node_at_all_is_refused(self, tmp_path: Path) -> None:
        assert _run_with_node(tmp_path, None) != 0

    def test_a_broken_node_binary_is_refused_not_crashed_on(self, tmp_path: Path) -> None:
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
            encoding="utf-8",
            timeout=30,
        )
        assert out.returncode != 0, "a broken node binary was treated as supported"


class TestFrontendBuildHeadroom:
    def test_the_build_raises_the_v8_heap_ceiling(self) -> None:
        """V8's ~2 GB default is not enough for this bundle; the OOM shows up
        as a build that dies with no clear cause and no dist/."""
        text = INSTALL_SH.read_text(encoding="utf-8")
        assert "--max-old-space-size" in text
        # Set only as a default so an operator can still override it.
        assert 'NODE_OPTIONS="${NODE_OPTIONS:-' in text

    def test_the_floor_is_defined_once(self) -> None:
        """The constant is consulted by both detection and the post-install
        check; two definitions could disagree."""
        text = INSTALL_SH.read_text(encoding="utf-8")
        assert text.count("NODE_MIN_VERSION=") == 1
        assert "NODE_MIN_MAJOR" not in text


def _function_source(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}", start) + len("\n}")
    return text[start:end]


class TestDistroNodeIsRaisedToTheFloor:
    """A fresh Amazon Linux 2023 host has no node, so the dnf branch runs and
    dnf ships node 18. Kept as is, the dashboard build fails under it; the
    branch must swap it for a supported node."""

    def _run_node_to_floor(
        self, tmp_path: Path, distro_version: str
    ) -> subprocess.CompletedProcess[str]:
        text = INSTALL_SH.read_text(encoding="utf-8")
        distro = tmp_path / "distro"
        distro.mkdir()
        _write_node(distro / "node", distro_version)
        good = tmp_path / "ensured"
        good.mkdir()
        _write_node(good / "node", "v24.5.1")
        tools = tmp_path / "tools"
        tools.mkdir()
        for util in ("sed", "cut", "sh", "env", "cat"):
            real = shutil.which(util)
            if real:
                (tools / util).symlink_to(real)
        # ensure-node.sh stands in as a stub that puts a supported node first
        # on PATH, which is what the real helper does through node-bin-dir.
        script = "\n".join(
            [
                _helper_source(),
                'ok() { echo "OK $1"; }',
                'info() { echo "INFO $1"; }',
                'warn() { echo "WARN $1"; }',
                'NODE_VERSION="24"',
                f'install_node_via_ensure() {{ echo ENSURE; PATH="{good}:$PATH"; }}',
                _function_source(text, "node_to_floor"),
                "node_to_floor dnf",
                "",
            ]
        )
        bash = shutil.which("bash")
        if bash is None:  # pragma: no cover - POSIX hosts always have bash
            pytest.skip("bash not available")
        return subprocess.run(
            [bash, "-c", script],
            env={"PATH": f"{distro}:{tools}"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def test_an_under_floor_distro_node_escalates_to_ensure_node(self, tmp_path: Path) -> None:
        out = self._run_node_to_floor(tmp_path, "v18.20.8")
        assert "ENSURE" in out.stdout, out.stdout
        assert "OK Node.js v24.5.1 now active" in out.stdout, out.stdout
        assert "installed via dnf" not in out.stdout, out.stdout

    def test_a_supported_distro_node_is_kept(self, tmp_path: Path) -> None:
        out = self._run_node_to_floor(tmp_path, "v22.20.1")
        assert "ENSURE" not in out.stdout, out.stdout
        assert "OK Node.js v22.20.1 installed via dnf" in out.stdout, out.stdout

    def test_every_distro_branch_goes_through_the_floor_check(self) -> None:
        """apt, dnf and brew can each ship a node below the floor."""
        text = INSTALL_SH.read_text(encoding="utf-8")
        section = text[
            text.index('info "Checking Node.js…"') : text.index("fi # USE_MISE -eq 0 (Node.js)")
        ]
        for manager in ("apt", "dnf", "brew"):
            assert f"node_to_floor {manager}" in section, manager


def _write_node(path: Path, version: str) -> None:
    path.write_text(f'#!/bin/sh\n[ "$1" = "--version" ] && echo "{version}"\n')
    path.chmod(0o755)
