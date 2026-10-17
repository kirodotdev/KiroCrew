"""Which audio decoder wheel each desktop backend installs.

``packaging/build-desktop.sh`` installs ``imageio-ffmpeg`` into every backend but
the macOS Intel one, which resolves a system FFmpeg instead.
``desktop_decoder_requirement`` is the one place that decides this; the helpers
are extracted from the shipped script rather than copied, so editing the real ones
is what this test runs against.
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


def _requirement(os_name: str, host_arch: str, want_arch: str) -> str:
    script = "\n".join(
        [
            "set -euo pipefail",
            f"OS={os_name}",
            f"HOST_ARCH={host_arch}",
            _extract("is_macos_intel_backend"),
            _extract("desktop_decoder_requirement"),
            f'desktop_decoder_requirement "{want_arch}"',
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
def test_the_macos_intel_backend_installs_no_decoder(os_name, host_arch, want_arch):
    assert _requirement(os_name, host_arch, want_arch) == ""


@pytest.mark.parametrize(
    ("os_name", "host_arch", "want_arch"),
    [
        ("darwin", "arm64", "arm64"),  # arm64 half of the universal build
        ("darwin", "arm64", ""),
        ("linux", "x86_64", ""),
        ("linux", "aarch64", ""),
        # An x86_64 Linux build is not an Intel Mac, whatever the arch string says.
        ("linux", "x86_64", "x86_64"),
    ],
)
def test_every_other_backend_installs_the_pinned_decoder(os_name, host_arch, want_arch):
    assert _requirement(os_name, host_arch, want_arch) == "imageio-ffmpeg==0.6.0"
