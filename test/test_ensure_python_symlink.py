"""``ensure-python.sh`` must record a real interpreter path, not a symlink.

uv and mise expose the interpreter they manage as a symlink on PATH (typically
``~/.local/bin/python3.13``). python-build-standalone builds — what both of them
install — locate their stdlib relative to the *invoked* path's dirname instead of
following the symlink first, so a venv created from that symlink is written with
``home = ~/.local/bin`` in ``pyvenv.cfg``, looks for the stdlib under
``~/.local/lib/python3.13``, and aborts with "ModuleNotFoundError: No module
named 'encodings'" — which fails ensurepip and so ``make build``.

``make backend`` feeds the recorded path straight to ``python -m venv``, so
resolving symlinks before recording is what keeps that working.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="ensure-python.sh is a bash script; Windows uses a different bootstrap",
)

SCRIPT = Path(__file__).parent.parent / "ensure-python.sh"

# The interpreter names ensure-python.sh probes, in its own preference order.
# Symlinking all of them makes the test independent of which one it settles on.
_PROBED_NAMES = (
    "python3.13",
    "python3.12",
    "python3",
    "python",
)


def _record_via_symlinked_python(tmp_path: Path) -> str:
    """Run the script with every probed name symlinked; return the recorded path.

    Reproduces the uv/mise layout: the only pythons on PATH are symlinks, the
    shape that made the script record a path a venv cannot be built from.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name in _PROBED_NAMES:
        (fake_bin / name).symlink_to(sys.executable)

    home = tmp_path / "home"
    env = dict(os.environ)
    env["KIROCREW_HOME"] = str(home)
    # The symlinks must win the PATH search, but the script also shells out to
    # `mkdir`, so keep coreutils reachable.
    env["PATH"] = os.pathsep.join([str(fake_bin), "/usr/bin", "/bin"])

    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    recorded = (home / "python-bin").read_text().strip()
    assert recorded, f"nothing recorded; script said: {proc.stdout}{proc.stderr}"
    return recorded


def test_records_the_resolved_path_when_python_on_path_is_a_symlink(tmp_path):
    recorded = _record_via_symlinked_python(tmp_path)

    assert not Path(recorded).is_symlink(), (
        f"recorded a symlink ({recorded}) — a venv built from it gets "
        "`home = <the symlink's dirname>` in pyvenv.cfg and cannot find its stdlib"
    )
    assert Path(recorded).exists(), recorded


def test_a_venv_built_from_the_recorded_path_can_import_the_stdlib(tmp_path):
    """The reported symptom: ``make build`` died inside ensurepip.

    ``--copies`` is what makes this bite. macOS copies the interpreter into the
    venv by default for framework builds, leaving ``pyvenv.cfg``'s ``home`` as
    the only thing that says where the stdlib lives. Linux symlinks instead and
    so happens to survive a wrong ``home`` — without ``--copies`` this test
    would pass even with the bug present.
    """
    recorded = _record_via_symlinked_python(tmp_path)

    venv = tmp_path / "venv"
    subprocess.run(
        [recorded, "-m", "venv", "--copies", "--without-pip", str(venv)],
        check=True,
        capture_output=True,
        timeout=60,
    )

    proc = subprocess.run(
        [str(venv / "bin" / "python"), "-c", "import encodings; print('ok')"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ok" in proc.stdout
