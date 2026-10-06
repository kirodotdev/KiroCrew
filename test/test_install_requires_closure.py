"""The wheel-closure job's requirements file is setup.cfg's install_requires, per arch.

``scripts/ci/install_requires_closure.py`` reads ``[options] install_requires``
and writes the entries that apply to one Linux arch, markers removed, for
``pip download --only-binary=:all: --platform manylinux...``. pip evaluates
markers against the runner, not ``--platform``, so a wrong marker verdict here
either drops an entry the gate should check or asks for a wheel the target
never installs.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ci" / "install_requires_closure.py"

pytest.importorskip("packaging")


def _load():
    spec = importlib.util.spec_from_file_location("install_requires_closure", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


closure = _load()

SETUP_CFG = """\
[metadata]
name = demo

[options]
python_requires = >=3.12
install_requires =
    aiohttp>=3.9,<4
    # A full-line comment between continuation lines.
        # An indented comment.
    qrcode[pil]>=7.4,<9  # trailing comment

    truststore==0.10.4; sys_platform == "darwin"
    tzdata>=2024.1; platform_system == "Windows"
    pysqlite3-binary>=0.5.4; sys_platform == "linux" and platform_machine == "x86_64"
    uvloop>=0.19; platform_system != "Windows"

[options.extras_require]
dev =
    pytest
"""


def test_continuation_lines_and_comments() -> None:
    assert closure.install_requires(SETUP_CFG) == [
        "aiohttp>=3.9,<4",
        "qrcode[pil]>=7.4,<9",
        'truststore==0.10.4; sys_platform == "darwin"',
        'tzdata>=2024.1; platform_system == "Windows"',
        'pysqlite3-binary>=0.5.4; sys_platform == "linux" and platform_machine == "x86_64"',
        'uvloop>=0.19; platform_system != "Windows"',
    ]


def test_extras_section_is_not_read() -> None:
    assert "pytest" not in closure.install_requires(SETUP_CFG)


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        (
            "x86_64",
            ["aiohttp<4,>=3.9", "qrcode[pil]<9,>=7.4", "pysqlite3-binary>=0.5.4", "uvloop>=0.19"],
        ),
        ("aarch64", ["aiohttp<4,>=3.9", "qrcode[pil]<9,>=7.4", "uvloop>=0.19"]),
    ],
)
def test_markers_are_evaluated_for_the_target_arch(machine: str, expected: list[str]) -> None:
    entries = closure.install_requires(SETUP_CFG)
    assert closure.requirements_for(entries, machine) == expected


def test_main_writes_the_requirements_file(tmp_path: Path) -> None:
    cfg = tmp_path / "setup.cfg"
    cfg.write_text(SETUP_CFG, encoding="utf-8")
    out = tmp_path / "reqs.txt"
    assert closure.main(["--machine", "aarch64", "--out", str(out), "--setup-cfg", str(cfg)]) == 0
    assert out.read_text(encoding="utf-8") == "aiohttp<4,>=3.9\nqrcode[pil]<9,>=7.4\nuvloop>=0.19\n"


def test_main_refuses_a_missing_install_requires(tmp_path: Path) -> None:
    cfg = tmp_path / "setup.cfg"
    cfg.write_text("[options]\nzip_safe = True\n", encoding="utf-8")
    out = tmp_path / "reqs.txt"
    assert closure.main(["--machine", "x86_64", "--out", str(out), "--setup-cfg", str(cfg)]) == 1
    assert not out.exists()


def test_the_repo_setup_cfg_parses() -> None:
    entries = closure.install_requires((ROOT / "setup.cfg").read_text(encoding="utf-8"))
    assert entries
    for machine in ("x86_64", "aarch64"):
        assert closure.requirements_for(entries, machine)
