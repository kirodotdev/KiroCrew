"""``ensure-python.sh`` must not record an interpreter whose venvs cannot run.

``make backend`` builds its venv with ``"$PY" -m venv`` from the recorded path.
Toolbox's kirocrew Python is a shared-libpython build behind a shell wrapper
(``exec ld-linux --library-path <lib> bin/real/python3.12``): it runs, clears
the 3.12 floor and reports the wrapper as ``sys.executable``, but the venv it
makes links ``bin/python`` to the real binary, which dies with
"libpython3.12.so.1.0: cannot open shared object file" (exit 127) -- so
ensurepip, and the whole build, fails. The script has to try a venv before it
records a candidate, and fall through to mise when that venv is broken.

The fake interpreter below reproduces exactly those properties: it forwards to
the real python (so the version probes pass), reports itself as
``sys.executable`` (so symlink resolution keeps it), and -- when ``broken`` --
writes a venv whose python exits 127 the way the Toolbox one does.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name == "nt",
    reason="ensure-python.sh is a bash script; Windows uses a different bootstrap",
)

SCRIPT = Path(__file__).parent.parent / "ensure-python.sh"

# Every name ensure-python.sh probes, so no host interpreter is ever reached.
_PROBED_NAMES = ("python3.12", "python3.13", "python3", "python")

_REAL = os.path.realpath(sys.executable)


def _write_fake_python(path: Path, *, broken: bool) -> None:
    real = shlex.quote(_REAL)
    self_literal = repr(str(path))
    venv_branch = (
        # Mimic the Toolbox venv: creation succeeds, its python cannot load.
        'if [ "$1" = "-m" ] && [ "$2" = "venv" ]; then\n'
        '    dir="${@: -1}"\n'
        '    mkdir -p "$dir/bin"\n'
        "    printf '#!/bin/sh\\necho \"error while loading shared libraries: "
        "libpython3.12.so.1.0: cannot open shared object file\" >&2\\nexit 127\\n'"
        ' > "$dir/bin/python"\n'
        '    chmod +x "$dir/bin/python"\n'
        "    exit 0\n"
        "fi\n"
        if broken
        else ""
    )
    path.write_text(
        "#!/bin/bash\n"
        + venv_branch
        # A wrapper reports itself, not the binary it execs, as sys.executable.
        + 'if [ "$1" = "-c" ]; then\n'
        + '    code="$2"; shift 2\n'
        + f'    exec {real} -c "import sys; sys.executable = {self_literal}\n$code" "$@"\n'
        + "fi\n"
        + f'exec {real} "$@"\n'
    )
    path.chmod(0o755)


def _write_mise_stub(home: Path, *, python: str) -> Path:
    """A mise that is already installed and answers ``which python`` with *python*."""
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    calls = home / "mise-calls"
    stub = bin_dir / "mise"
    stub.write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> {shlex.quote(str(calls))}\n'
        f'if [ "$1" = "which" ]; then printf "%s\\n" {shlex.quote(python)}; fi\n'
        "exit 0\n"
    )
    stub.chmod(0o755)
    return calls


def _run(tmp_path: Path, *, broken: bool, mise_python: str, extra_env=None):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "python3.12"
    _write_fake_python(fake, broken=broken)
    for name in _PROBED_NAMES[1:]:
        (fake_bin / name).symlink_to(fake)

    user_home = tmp_path / "user"
    calls = _write_mise_stub(user_home, python=mise_python)
    data_home = tmp_path / "data"
    env = dict(os.environ)
    env["HOME"] = str(user_home)
    env["KIROCREW_HOME"] = str(data_home)
    env["PATH"] = os.pathsep.join([str(fake_bin), "/usr/bin", "/bin"])
    env.update(extra_env or {})

    proc = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    marker = data_home / "python-bin"
    recorded = marker.read_text().strip() if marker.exists() else ""
    return proc, fake, recorded, calls


def test_an_interpreter_whose_venv_cannot_run_is_skipped_for_mise(tmp_path):
    proc, fake, recorded, _ = _run(tmp_path, broken=True, mise_python=_REAL)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert recorded != str(fake), (
        "recorded an interpreter whose venv python cannot start -- "
        "`make backend` would then fail inside ensurepip"
    )
    assert recorded == _REAL, proc.stdout + proc.stderr
    assert f"skipping {fake}" in proc.stderr


def test_a_working_wrapper_is_still_recorded(tmp_path):
    """Control: the venv probe rejects broken venvs, not wrappers as such."""
    proc, fake, recorded, calls = _run(tmp_path, broken=False, mise_python=_REAL)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert recorded == str(fake), proc.stdout + proc.stderr
    assert not calls.exists(), "mise was consulted although the system python works"


def test_an_unusable_tmpdir_does_not_reject_a_working_interpreter(tmp_path):
    """No scratch directory for the probe is not evidence the interpreter is broken."""
    proc, fake, recorded, calls = _run(
        tmp_path,
        broken=False,
        mise_python=_REAL,
        extra_env={"TMPDIR": str(tmp_path / "missing")},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert recorded == str(fake), proc.stdout + proc.stderr
    assert not calls.exists(), "mise was consulted although the system python works"


def test_nothing_is_recorded_when_no_candidate_can_build_a_venv(tmp_path):
    # mise has no python to offer, so every route ends at a broken interpreter.
    proc, fake, recorded, _ = _run(tmp_path, broken=True, mise_python="")

    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert recorded == "", f"recorded {recorded!r} although no venv could run"
