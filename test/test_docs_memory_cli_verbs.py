"""Every ``kirocrew memory <verb>`` the docs name is a verb the CLI registers.

A spec or code comment that names a memory verb the parser does not have sends
the reader to a command that fails with ``invalid choice``. The verb set comes
from :func:`kiro_crew.cli_parser_agent.register_memory_parser` itself, so a new
or renamed verb needs no edit here.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from kiro_crew.cli_parser_agent import register_memory_parser

REPO = Path(__file__).resolve().parent.parent

# Markdown under the two doc trees, and double-backticked spans in source
# comments and docstrings. A bare ``kirocrew memory x`` in a .py file is often a
# log message rather than a command reference, so source is read in backticks only.
_DOC_VERB = re.compile(r"\bkirocrew memory ([a-z][a-z-]*)")
_SRC_VERB = re.compile(r"``kirocrew memory ([a-z][a-z-]*)")


def _registered_memory_verbs() -> set[str]:
    root = argparse.ArgumentParser()
    register_memory_parser(root.add_subparsers(dest="command"))
    mem_parser = next(
        a for a in root._actions if isinstance(a, argparse._SubParsersAction)
    ).choices["memory"]
    mem_sub = next(a for a in mem_parser._actions if isinstance(a, argparse._SubParsersAction))
    return set(mem_sub.choices)


def _named_verbs() -> dict[str, list[str]]:
    named: dict[str, list[str]] = {}
    doc_files = [*(REPO / "docs").rglob("*.md"), *(REPO / "src/kiro_crew/docs").rglob("*.md")]
    for path in doc_files:
        for verb in _DOC_VERB.findall(path.read_text(encoding="utf-8")):
            named.setdefault(verb, []).append(str(path.relative_to(REPO)))
    for path in (REPO / "src/kiro_crew").rglob("*.py"):
        for verb in _SRC_VERB.findall(path.read_text(encoding="utf-8")):
            named.setdefault(verb, []).append(str(path.relative_to(REPO)))
    return named


def test_registered_verb_set_is_read_from_the_parser() -> None:
    verbs = _registered_memory_verbs()
    assert {"list", "search", "backups"} <= verbs


def test_every_documented_memory_verb_is_registered() -> None:
    registered = _registered_memory_verbs()
    unknown = {verb: files for verb, files in _named_verbs().items() if verb not in registered}
    assert not unknown, f"docs name memory verbs the CLI does not register: {unknown}"
