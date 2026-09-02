"""Guard: no source in this repo parses YAML through ``yaml.load``.

``yaml.load`` is only as safe as its ``Loader=`` argument, and that safety is
invisible at the call site: a reviewer — and every static scanner that matches
on the call name — has to chase the loader class to tell an untrusted-input RCE
apart from a deliberate CloudFormation tag-tolerant parse. So the repo parses
either with ``yaml.safe_load``, or by driving a ``yaml.SafeLoader`` subclass
directly (``load_with`` in `test/yaml_helpers.py`, ``_load_no_alias_yaml`` in
``onboarding_import``), which makes the safe base class the only construction
path and needs no per-line suppression comment to stay quiet.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from yaml_helpers import load_with

_REPO = Path(__file__).resolve().parents[1]

# Built at runtime so this guard's own source does not contain the needles.
# ``full_load`` is fenced alongside the obvious ones: FullLoader is the
# historically exploitable path (it still constructs arbitrary Python objects
# through tags), so leaving it out would let a future call site walk straight
# through this fence.
_NEEDLES = tuple(
    f"yaml.{name}("
    for name in (
        "load",
        "load_all",
        "unsafe_load",
        "unsafe_load_all",
        "full_load",
        "full_load_all",
    )
)

# The bundled llama.cpp binding is third-party source, not ours to restyle.
_SKIP_DIRS = ("_vendor", "__pycache__", "node_modules")

# This module asserts the ABSENCE of the pattern in another module, so the
# needle appears there as string data rather than as a call.
_ALLOWLIST = frozenset(
    {
        "src/kiro_crew/apps/builtins/ops_mission_control/tests/test_schedule_file.py",
    }
)


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for root in (_REPO / "src", _REPO / "test"):
        for path in root.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in path.parts):
                continue
            if path.relative_to(_REPO).as_posix() in _ALLOWLIST:
                continue
            files.append(path)
    return files


def test_no_yaml_load_call_sites() -> None:
    offenders: list[str] = []
    for path in _candidate_files():
        source = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(source.splitlines(), start=1):
            if any(needle in line for needle in _NEEDLES):
                offenders.append(f"{path.relative_to(_REPO).as_posix()}:{lineno}")
    assert not offenders, (
        "parse YAML with yaml.safe_load, or drive a yaml.SafeLoader subclass "
        f"directly (see test/yaml_helpers.py load_with): {offenders}"
    )


def test_load_with_parses_through_a_safe_subclass() -> None:
    class _TagTolerant(yaml.SafeLoader):
        pass

    _TagTolerant.add_multi_constructor(
        None, lambda loader, suffix, node: loader.construct_scalar(node)
    )

    doc = load_with(_TagTolerant, "Name: !Sub value\n")
    assert doc == {"Name": "value"}


def test_load_with_refuses_an_unsafe_loader() -> None:
    with pytest.raises(TypeError):
        load_with(yaml.UnsafeLoader, "a: 1\n")
