"""Importing the crew bundle builder imports every ``pipeline`` owner, before a test can patch.

``packaging/build.py`` is a facade: every name it serves is read from the ``pipeline`` owner
that defines it. An owner binds what it imports by name when it first runs --
``pipeline/scan.py`` takes ``redact_credentials`` from ``kiro_crew.security`` -- so an owner
loaded on the facade's first read, inside a test's patch of that source, would keep the
patched value for the rest of the worker. The facade therefore imports every owner at the
end of its body.

Each case runs in a fresh interpreter: in this one, an earlier test has usually loaded every
owner already, and the property would hold whether or not the facade imports them.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from kiro_crew.subprocess_utf8 import UTF8_TEXT

_SRC = Path(__file__).resolve().parents[1] / "src"
_CREW_ROOT = _SRC / "kiro_crew" / "apps" / "builtins" / "aws_control" / "crew"
_BUILD_PY = _CREW_ROOT / "packaging" / "build.py"
_FACADE = "kiro_crew.apps.builtins.aws_control.crew.packaging.build"

#: The owners on disk, read without the facade, so an emptied export table cannot pass.
_OWNERS = sorted(
    path.stem
    for path in (_CREW_ROOT / "packaging" / "pipeline").glob("*.py")
    if path.stem != "__init__"
)


def _child(argv: list[str], cwd: Path, *path: Path) -> subprocess.CompletedProcess[str]:
    """Run a fresh interpreter in *cwd* with *path* ahead of the inherited import path."""
    env = dict(os.environ)
    entries = [*map(str, path), env.get("PYTHONPATH", "")]
    env["PYTHONPATH"] = os.pathsep.join(entry for entry in entries if entry)
    return subprocess.run(
        [sys.executable, *argv],
        capture_output=True,
        check=False,
        cwd=str(cwd),
        env=env,
        timeout=60,
        **UTF8_TEXT,
    )


def test_importing_the_builder_binds_every_owner_before_a_test_can_patch(tmp_path: Path) -> None:
    """A fresh interpreter that imports the facade, patches ``kiro_crew.security``'s
    redactor, reads a moved name and undoes the patch must find every owner already
    loaded and ``scan`` holding the real redactor."""
    code = (
        "import sys\n"
        "from kiro_crew import security\n"
        f"import {_FACADE} as build\n"
        f"owners = {_OWNERS!r}\n"
        "print(sorted(set(build._EXPORTS.values())) == ['pipeline.' + o for o in owners])\n"
        "prefix = build.__package__ + '.pipeline.'\n"
        "print(','.join(o for o in owners if prefix + o not in sys.modules))\n"
        "real = security.redact_credentials\n"
        "security.redact_credentials = lambda text: (text, [])\n"
        "build.scan_text\n"
        "security.redact_credentials = real\n"
        "scan = sys.modules[prefix + 'scan']\n"
        "print(scan._CANONICAL_REDACTOR is real, build.redact_credentials is real)\n"
    )
    assert len(_OWNERS) > 1 and "scan" in _OWNERS
    out = _child(["-c", code], tmp_path, _SRC)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines() == ["True", "", "True True"], out.stdout + out.stderr


def test_the_documented_entries_still_hold_with_owners_imported_eagerly(tmp_path: Path) -> None:
    """``python -m packaging.build`` resolves its owners against ``__package__``; run by file
    path there is none, so the facade imports no owner and the main guard refuses."""
    entry = _child(["-m", "packaging.build", "--help"], tmp_path, _CREW_ROOT, _SRC)
    assert entry.returncode == 0, entry.stdout + entry.stderr
    assert entry.stdout.startswith("usage: python -m packaging.build"), entry.stdout
    by_path = _child([str(_BUILD_PY)], tmp_path)
    assert by_path.returncode == 2, by_path.stdout + by_path.stderr
    assert by_path.stderr.startswith(
        "refused: run the crew bundle builder as `python -m packaging.build`"
    ), by_path.stderr
