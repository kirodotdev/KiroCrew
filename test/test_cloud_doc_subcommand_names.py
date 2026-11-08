"""Every ``cloud <sub>`` the cloud spec and the cloud code name is one the parser registers.

A reader who copies ``kirocrew cloud <sub>`` from the spec or a docstring and gets an
argparse error has been sent to a command that does not exist. The command that
re-attaches to an existing launch is ``cloud launch``; there is no ``cloud resume``. This
test reads the subcommand set from ``register_cloud_parser`` itself, so a renamed or
removed subcommand fails here too.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from kiro_crew.cli_parser_admin import register_cloud_parser

REPO = Path(__file__).resolve().parent.parent

# The texts that describe the shipped `kirocrew cloud` command. Guides are left out on
# purpose: one names a `cloud config` command it says does not exist yet.
SCANNED = [
    REPO / "docs/system-specs/modules/cloud.md",
    REPO / "src/kiro_crew/cli_cloud.py",
    *sorted((REPO / "src/kiro_crew/cloud").rglob("*.py")),
]

# `cloud <sub>` inside a code span (`...` or ``...``), with or without `kirocrew `.
_NAMED_SUBCOMMAND = re.compile(r"`(?:kirocrew\s+)?cloud\s+([a-z][a-z-]*)")


def _registered_cloud_subcommands() -> set[str]:
    root = argparse.ArgumentParser()
    register_cloud_parser(root.add_subparsers(dest="command"))
    cloud = next(
        action.choices["cloud"]
        for action in root._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    sub = next(a for a in cloud._actions if isinstance(a, argparse._SubParsersAction))
    return set(sub.choices)


def _named_subcommands() -> dict[str, list[str]]:
    named: dict[str, list[str]] = {}
    for path in SCANNED:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in _NAMED_SUBCOMMAND.finditer(line):
                where = f"{path.relative_to(REPO).as_posix()}:{lineno}"
                named.setdefault(match.group(1), []).append(where)
    return named


def test_the_parser_registers_the_subcommands_this_test_relies_on():
    registered = _registered_cloud_subcommands()
    assert {"launch", "destroy", "connect"} <= registered


def test_every_named_cloud_subcommand_is_registered():
    registered = _registered_cloud_subcommands()
    unknown = {
        name: sites for name, sites in _named_subcommands().items() if name not in registered
    }
    assert unknown == {}, (
        "These texts name a `kirocrew cloud` subcommand the parser does not register "
        f"(registered: {sorted(registered)}): {unknown}"
    )


def test_the_scan_sees_the_spec():
    named = _named_subcommands()
    assert any(
        site.startswith("docs/system-specs/modules/cloud.md") for site in named.get("launch", [])
    )
