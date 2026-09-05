"""Every sigil-less channel command alias must be one the marker strip refuses.

The sigil guard is a PREFIX test, so it cannot see a command that carries no sigil at
all. WeCom and Weixin accept CJK spellings (`清空`, `新对话`, ...) by exact equality, so
stripping a marker off `(recommended) 清空` hands the channel the whole command and the
user's click resets the session instead of answering.

``_RESERVED_TEXT_COMMANDS`` is a literal list because importing the channel command
modules into ``constants`` would cycle. This test is what makes the literal honest: it
reads every channel's declared aliases and fails when one of them is sigil-less and
undeclared, so a new CJK alias cannot land without the guard learning about it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kiro_crew.constants import (
    _RESERVED_DISPATCH_SIGILS,
    _RESERVED_TEXT_COMMANDS,
    strip_recommended_marker,
)

_COMMANDS = sorted(
    (Path(__file__).resolve().parents[1] / "src" / "kiro_crew").glob("*/commands.py")
)
# Alias literals inside a frozenset(...) / aliases=(...) construct.
_ALIAS_BLOCK_RE = re.compile(
    r"(?:_?[A-Z_]*ALIASES\s*=\s*frozenset\(|aliases\s*=\s*)[\(\{]([^)}]*)[\)\}]",
    re.DOTALL,
)
_LITERAL_RE = re.compile(r"""(['"])(?P<value>(?:[^'"\\]|\\.)+)\1""")


def _declared_aliases() -> dict[str, set[str]]:
    """Every alias literal each channel declares, keyed by channel."""
    found: dict[str, set[str]] = {}
    for path in _COMMANDS:
        source = path.read_text(encoding="utf-8")
        aliases = {
            m.group("value")
            for block in _ALIAS_BLOCK_RE.finditer(source)
            for m in _LITERAL_RE.finditer(block.group(1))
        }
        if aliases:
            found[path.parent.name] = aliases
    return found


def _sigil_less(aliases: set[str]) -> set[str]:
    return {a for a in aliases if a and not a.startswith(tuple(_RESERVED_DISPATCH_SIGILS) + ("!",))}


class TestTheScanCanSeeTheChannels:
    def test_it_finds_alias_declarations_in_several_channels(self):
        # Guards the guard: a broken regex would report every channel clean.
        declared = _declared_aliases()
        assert len(declared) >= 5, sorted(declared)

    def test_it_finds_the_known_cjk_aliases(self):
        # Positive control on the exact vector the finding names.
        declared = _declared_aliases()
        assert "清空" in declared.get("wecom", set())
        assert "清空" in declared.get("weixin", set())


class TestNoChannelAcceptsASigilLessCommandTheGuardIgnores:
    @pytest.mark.parametrize("channel", sorted(_declared_aliases()))
    def test_every_sigil_less_alias_is_declared(self, channel):
        undeclared = {
            alias
            for alias in _sigil_less(_declared_aliases()[channel])
            if alias not in _RESERVED_TEXT_COMMANDS
            and alias.casefold() not in _RESERVED_TEXT_COMMANDS
        }
        assert not undeclared, (
            f"{channel} accepts {sorted(undeclared)!r} with no sigil, so a stripped "
            f"`(recommended) {sorted(undeclared)[0]}` would dispatch as a command. "
            "Add it to _RESERVED_TEXT_COMMANDS."
        )


class TestTheStripRefusesThem:
    @pytest.mark.parametrize("alias", sorted(_RESERVED_TEXT_COMMANDS))
    def test_a_marked_command_alias_is_left_verbatim(self, alias):
        label = f"(recommended) {alias}"
        assert strip_recommended_marker(label) == label

    def test_an_alias_inside_a_longer_label_still_strips(self):
        # Exact equality only: the channels match the WHOLE message, so a sentence
        # merely containing the word is ordinary text and must keep its badge.
        assert strip_recommended_marker("(recommended) 清空这个列表吗") == "清空这个列表吗"
