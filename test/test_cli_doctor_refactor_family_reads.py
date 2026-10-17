"""A doctor family reads every project module it uses through ``cli_doctor``, at call time.

The families load lazily, on the first read through the facade, so an import that binds a
project module by name in a family (``from kiro_crew import sandbox``) would take that
value at whatever moment the first read falls -- inside a test's rebind of
``kiro_crew.sandbox``, say -- and keep it for the rest of the process. It would also stop a
patch of the facade's own binding (``cli_doctor.sandbox``) from reaching the family, which
the one-module report honoured. So a family binds no project module but the facade and
its sibling families, and reads ``cli_doctor.<module>.<name>`` where it needs one.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from kiro_crew import cli_doctor, doctor_checks
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_FAMILIES_DIR = Path(doctor_checks.__file__).resolve().parent


def _by_name_project_imports(source: str) -> list[str]:
    """Module-scope imports outside ``TYPE_CHECKING`` that bind a project name a family
    would hold as its own: anything from ``kiro_crew`` but the facade and the families."""
    hits = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Import):
            hits += [
                f"{node.lineno}: {a.name}" for a in node.names if a.name.startswith("kiro_crew")
            ]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "kiro_crew":
                names = [a.name for a in node.names if a.name != "cli_doctor"]
            elif node.module.startswith("kiro_crew") and node.module != doctor_checks.__name__:
                names = [a.name for a in node.names]
            else:
                names = []
            hits += [f"{node.lineno}: {name}" for name in names]
    return hits


def test_no_family_binds_a_project_module_or_name_by_name() -> None:
    families = sorted(_FAMILIES_DIR.glob("*.py"))
    # Non-vacuous: every family the facade forwards to is a file this scan reads.
    stems = {path.stem for path in families}
    assert {name.rpartition(".")[2] for name in cli_doctor._EXPORTS.values()} <= stems
    offenders = {
        path.name: hits
        for path in families
        if (hits := _by_name_project_imports(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_the_by_name_scan_catches_a_planted_import() -> None:
    planted = (
        "from typing import TYPE_CHECKING\n"
        "from kiro_crew import cli_doctor, sandbox\n"
        "from kiro_crew.doctor_checks import render\n"
        "from kiro_crew.service import linux as service_linux\n"
        "from kiro_crew.config.paths import config_dir\n"
        "import kiro_crew.stt\n"
        "if TYPE_CHECKING:\n"
        "    from kiro_crew.discord import intent_probe\n"
    )
    assert _by_name_project_imports(planted) == [
        "2: sandbox",
        "4: linux",
        "5: config_dir",
        "6: kiro_crew.stt",
    ]


def test_a_patch_of_the_facade_module_reaches_the_family(monkeypatch) -> None:
    class _Sandbox:
        @staticmethod
        def _mount_source_candidate_roots() -> list[str]:
            return ["/patched-through-the-facade"]

    monkeypatch.setattr(cli_doctor, "sandbox", _Sandbox)
    assert cli_doctor._runtime_tmpfs_roots() == ["/patched-through-the-facade"]


def test_a_family_first_read_inside_a_package_rebind_keeps_nothing_of_it(tmp_path) -> None:
    """In a fresh interpreter the family is not loaded yet, so the first read through the
    facade, made while ``kiro_crew.sandbox`` is rebound, is the moment it loads. Once the
    rebind is undone the family must read the real module, and a facade patch made after
    the load must still reach it."""
    family = "kiro_crew.doctor_checks.resources"
    code = (
        "import json, sys\n"
        "import kiro_crew\n"
        "import kiro_crew.cli_doctor as doctor\n"
        f"loaded_before = {family!r} in sys.modules\n"
        "real = doctor.sandbox\n"
        "kiro_crew.sandbox = object()\n"
        "doctor._runtime_tmpfs_roots\n"
        "kiro_crew.sandbox = real\n"
        f"held = vars(sys.modules[{family!r}]).get('sandbox')\n"
        "class Stub:\n"
        "    @staticmethod\n"
        "    def _mount_source_candidate_roots():\n"
        "        return ['/stub']\n"
        "doctor.sandbox = Stub\n"
        "try:\n"
        "    patched = doctor._runtime_tmpfs_roots()\n"
        "except Exception as exc:\n"
        "    patched = type(exc).__name__\n"
        "doctor.sandbox = real\n"
        "print(json.dumps({'loaded_before': loaded_before,\n"
        "                  'held': None if held is None else type(held).__name__,\n"
        "                  'patched': patched}))\n"
    )
    env = dict(os.environ)
    src = str(Path(cli_doctor.__file__).resolve().parents[1])
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        check=False,
        cwd=tmp_path,
        env=env,
        timeout=120,
        **UTF8_TEXT,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout.splitlines()[-1]) == {
        "loaded_before": False,
        "held": None,
        "patched": ["/stub"],
    }


@pytest.mark.parametrize(
    "name", ["sandbox", "platform_compat", "session_pid", "stt", "apparmor", "intent_probe"]
)
def test_the_facade_holds_each_module_the_families_read(name) -> None:
    assert isinstance(vars(cli_doctor)[name], ModuleType)
