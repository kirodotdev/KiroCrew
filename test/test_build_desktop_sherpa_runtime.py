"""Every desktop backend bundles the sherpa-onnx runtime, wheel-only, and checks it.

``packaging/build-desktop.sh`` installs the ``voice-sherpa`` extra through
``install_sherpa_runtime`` and verifies it after pruning with
``sherpa_runtime_gate``. The helpers are extracted from the shipped script and run
against a fake interpreter, so editing the real ones is what this test runs against.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "packaging" / "build-desktop.sh"


def _text() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _extract(name: str) -> str:
    match = re.search(rf"^{name}\(\) \{{.*?^\}}", _text(), re.DOTALL | re.MULTILINE)
    assert match, f"{name}() not found in packaging/build-desktop.sh"
    return match.group(0)


def _function_body(name: str) -> str:
    """The body of the shell function *name*, up to the next top-level function."""
    match = re.search(rf"^{name}\(\) \{{\n(.*?)^\}}\n", _text(), re.DOTALL | re.MULTILINE)
    assert match, f"{name}() not found in packaging/build-desktop.sh"
    return match.group(1)


def _backend_builders() -> list[str]:
    """The functions that install voice extras into a bundled interpreter."""
    names = re.findall(r"^(build_backend\w*)\(\) \{$", _text(), re.MULTILINE)
    return [n for n in names if "[voice" in _function_body(n)]


def test_both_backend_builders_install_and_gate_the_runtime():
    builders = _backend_builders()
    # The POSIX builder and the Windows one.
    assert len(builders) == 2, builders
    for name in builders:
        body = _function_body(name)
        assert "install_sherpa_runtime " in body, name
        assert "[voice-sherpa]" in body, name
        # The gate runs after the prune, so it checks what ships.
        assert body.index("sherpa_runtime_gate ") > body.index("Pruning bundle"), name


def test_the_macos_intel_backend_is_gated_too():
    """Unlike the whisper gate, the sherpa one is not skipped for macOS Intel."""
    body = _function_body(_backend_builders()[0])
    gate = body.index("sherpa_runtime_gate ")
    guard = body.rindex("is_macos_intel_backend", 0, gate)
    assert "fi" in body[guard:gate]


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX bash helpers")
def test_the_install_is_wheel_only(tmp_path):
    fake_python = tmp_path / "python"
    fake_python.write_text('#!/bin/sh\nprintf "%s " "$@"\n', encoding="utf-8")
    fake_python.chmod(0o755)
    script = "\n".join(
        [
            "set -euo pipefail",
            _extract("install_sherpa_runtime"),
            f'install_sherpa_runtime "{fake_python}" "/root[voice-sherpa]"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, encoding="utf-8", check=True
    )
    argv = result.stdout.split()
    assert argv[:3] == ["-m", "pip", "install"]
    assert argv[argv.index("--only-binary") + 1] == "sherpa-onnx,sherpa-onnx-core"
    assert argv[-1] == "/root[voice-sherpa]"


@pytest.mark.skipif(os.name == "nt", reason="exercises POSIX bash helpers")
@pytest.mark.parametrize("exit_code, ok", [(0, True), (1, False)])
def test_the_gate_fails_the_build_when_the_runtime_cannot_load(tmp_path, exit_code, ok):
    fake_python = tmp_path / "python"
    fake_python.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
    fake_python.chmod(0o755)
    script = "\n".join(
        [
            "set -euo pipefail",
            "log() { :; }",
            _extract("sherpa_runtime_gate"),
            f'sherpa_runtime_gate "{fake_python}"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, encoding="utf-8"
    )
    assert (result.returncode == 0) is ok
    if not ok:
        assert "Sherpa-ONNX runtime cannot load" in result.stderr


def test_the_gate_imports_what_the_provider_needs():
    gate = _extract("sherpa_runtime_gate")
    assert "import bz2" in gate
    assert "import sherpa_onnx" in gate
    assert "from kiro_crew.stt.sherpa_engine import probe" in gate
