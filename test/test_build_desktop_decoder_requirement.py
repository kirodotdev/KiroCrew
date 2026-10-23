"""Which desktop backends get the pinned audio decoder installed.

``packaging/build-desktop.sh`` installs the decoder into every backend but the
macOS Intel one, which resolves a system FFmpeg instead.
``install_desktop_decoder`` is the one place that decides this, and it must get
the bytes from ``kiro_crew.stt.decoder``'s own pin table. The helpers are
extracted from the shipped script rather than copied, so editing the real ones is
what this test runs against.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="exercises POSIX bash helpers")

SCRIPT = Path(__file__).parent.parent / "packaging" / "build-desktop.sh"


def _extract(name: str) -> str:
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{.*?^\}}", text, re.DOTALL | re.MULTILINE)
    assert match, f"{name}() not found in packaging/build-desktop.sh"
    return match.group(0)


def _install_argv(tmp_path: Path, os_name: str, host_arch: str, want_arch: str) -> str:
    """The argv ``install_desktop_decoder`` hands the bundled interpreter, or ""."""
    fake_python = tmp_path / "python"
    # Records what it was asked to run instead of installing anything.
    fake_python.write_text('#!/bin/sh\nprintf "%s " "$@"\n', encoding="utf-8")
    fake_python.chmod(0o755)
    script = "\n".join(
        [
            "set -euo pipefail",
            f"OS={os_name}",
            f"HOST_ARCH={host_arch}",
            "log() { :; }",
            _extract("is_macos_intel_backend"),
            _extract("install_desktop_decoder"),
            f'install_desktop_decoder "{fake_python}" /sp "{want_arch}"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, encoding="utf-8", check=True
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("os_name", "host_arch", "want_arch"),
    [
        ("darwin", "arm64", "x86_64"),  # Intel half of the universal build
        ("darwin", "x86_64", ""),  # host-arch build on an Intel Mac
        ("darwin", "x86_64", "x86_64"),  # named Intel build on an Intel Mac
    ],
)
def test_the_macos_intel_backend_installs_no_decoder(tmp_path, os_name, host_arch, want_arch):
    assert _install_argv(tmp_path, os_name, host_arch, want_arch) == ""


@pytest.mark.parametrize(
    ("os_name", "host_arch", "want_arch"),
    [
        ("darwin", "arm64", "arm64"),  # arm64 half of the universal build
        ("darwin", "arm64", ""),
        ("linux", "x86_64", ""),
        ("linux", "aarch64", ""),
        # An x86_64 Linux build is not an Intel Mac, whatever the arch string says.
        ("linux", "x86_64", "x86_64"),
        ("windows", "x86_64", ""),
    ],
)
def test_every_other_backend_installs_the_pinned_decoder(tmp_path, os_name, host_arch, want_arch):
    argv = _install_argv(tmp_path, os_name, host_arch, want_arch)
    # The pin table's own installer, into the directory the runtime resolves.
    assert argv == "-s -m kiro_crew.stt.decoder --install-into /sp/kirocrew_ffmpeg/bin"


def test_the_install_dir_is_the_one_the_runtime_resolves() -> None:
    from kiro_crew.stt import decoder

    assert "/".join(decoder.PACKAGED_DIR) == "kirocrew_ffmpeg/bin"
    assert '"$2/kirocrew_ffmpeg/bin"' in _extract("install_desktop_decoder")


def test_no_build_path_installs_imageio_ffmpeg() -> None:
    assert "imageio-ffmpeg" not in SCRIPT.read_text(encoding="utf-8")
