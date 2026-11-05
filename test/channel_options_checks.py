"""Checks the Discord and Telegram test files share, written once.

Each check lives here as a mixin. A test class in ``test_discord.py`` or
``test_telegram.py`` takes the mixin as a base, so the node id stays
``<file>::<Class>::<test>``, the class keeps its own setup, and pytest resolves the
test's fixtures from that file. A check that reads the file's own constants or helpers
does so through :func:`_file_module`.
"""

from __future__ import annotations

import sys
from types import ModuleType

from conftest import assert_rejected_without_backtracking


def _file_module(test: object) -> ModuleType:
    """The test file the class running this check is defined in."""
    return sys.modules[type(test).__module__]


class UnterminatedOptionsTagIsNotRedos:
    def test_unterminated_options_tag_is_not_redos(self) -> None:
        # Regression (py/polynomial-redos): a plain greedy ``.*`` body could
        # consume a "[" that ALSO starts the outer "[OPTIONS:" literal, so over
        # text with many "[OPTIONS:" prefixes search() re-explored the body from
        # each position — polynomial. The tempered body
        # (?:[^[]|\[(?!OPTIONS:))* forbids only a re-occurring "[OPTIONS:", so
        # the body is unambiguous (linear). A whitespace-padded unterminated tag
        # and many repeated "[OPTIONS:" prefixes (the real pump) must both be
        # rejected in CPU time linear in the pump -- see
        # conftest.assert_rejected_without_backtracking for why this is not a
        # 1.0 s wall-clock bound.
        here = _file_module(self)

        def reject(text: str) -> None:
            body, opts = here._extract_options(text)
            assert opts == []

        assert_rejected_without_backtracking(reject, lambda n: "[OPTIONS:" + ("\t" * n) + "x")
        assert_rejected_without_backtracking(reject, lambda n: "[OPTIONS:" * n + "x")
