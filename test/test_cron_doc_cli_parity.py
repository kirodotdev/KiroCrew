"""The cron guide names every `kirocrew cron` verb and long flag the parser accepts.

The guide is what `search_docs` and the agents read to learn the CLI, so a verb or a
flag the parser has and the guide lacks is a command nobody is told about. The
parser is the source of truth: this walks it rather than a hand-kept list.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from kiro_crew.cli_parser_agent import register_cron_parser

_DOC = Path(__file__).resolve().parents[1] / "src/kiro_crew/docs/cron-and-scheduling.md"


def _cron_verbs() -> dict[str, argparse.ArgumentParser]:
    root = argparse.ArgumentParser(prog="kirocrew")
    register_cron_parser(root.add_subparsers(dest="command"))
    cron = next(a for a in root._actions if isinstance(a, argparse._SubParsersAction)).choices[
        "cron"
    ]
    sub = next(a for a in cron._actions if isinstance(a, argparse._SubParsersAction))
    return dict(sub.choices)


def _long_flags(parser: argparse.ArgumentParser) -> set[str]:
    return {
        opt
        for action in parser._actions
        for opt in action.option_strings
        if opt.startswith("--") and opt != "--help"
    }


def test_every_cron_verb_is_documented():
    doc = _DOC.read_text(encoding="utf-8")
    missing = sorted(v for v in _cron_verbs() if f"kirocrew cron {v}" not in doc)
    assert missing == [], f"cron-and-scheduling.md never shows: {missing}"


def test_managed_by_is_documented_for_add_and_remove():
    doc = _DOC.read_text(encoding="utf-8")
    verbs = _cron_verbs()
    for verb in ("add", "remove"):
        assert "--managed-by" in _long_flags(verbs[verb])
        assert f"kirocrew cron {verb} --managed-by" in doc, verb
