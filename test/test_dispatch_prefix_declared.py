"""A leading-prefix branch on the dashboard dispatch path must name itself as a sigil.

Marker stripping can change a label's FIRST character, so any dispatch path that reads a
leading prefix decides what a stripped label becomes. The two-list parity test only
catches the frontend and backend deny lists diverging from each other; it is blind to a
THIRD prefix appearing on the dispatch path and in neither list. Today the only tripwire
is a comment at each dispatcher asking the author to update the list, and a comment
cannot fail CI.

This scans the dispatch entry points for single-character leading prefixes and requires
each to be a declared sigil. It cannot see a dispatcher nobody added here, so
``DISPATCH_SITES`` is the maintained part -- but it turns "someone remembered the
comment" into "CI says so" for the prefixes on the paths we do know about.
"""

from __future__ import annotations

import inspect
import re

import pytest

from kiro_crew.constants import _RESERVED_DISPATCH_SIGILS
from kiro_crew.dashboard.chat_runner import _resolve_prompt_mention
from kiro_crew.dashboard.chat_utils import is_harness_slash_command

# Each entry is a dispatch entry point plus the prefix it is KNOWN to read, so a rename or
# a moved branch fails loudly here instead of silently scanning nothing.
DISPATCH_SITES = [
    (is_harness_slash_command, "/"),
    (_resolve_prompt_mention, "@"),
]

_STARTSWITH_RE = re.compile(r"""\.startswith\(\s*(['"])(?P<prefix>[^'"]{1})\1""")


def _leading_prefixes(func) -> set[str]:
    """Single-character prefixes the function tests for at the start of a string."""
    return {m.group("prefix") for m in _STARTSWITH_RE.finditer(inspect.getsource(func))}


class TestEveryLeadingDispatchPrefixIsADeclaredSigil:
    @pytest.mark.parametrize(
        "func,known", DISPATCH_SITES, ids=lambda v: getattr(v, "__name__", str(v))
    )
    def test_the_scan_still_finds_the_prefix_this_site_is_known_to_read(self, func, known):
        # Positive control: without this, a moved branch makes the assertion below vacuous.
        assert known in _leading_prefixes(func), (
            f"{func.__name__} no longer tests a leading {known!r} -- "
            "did the branch move? Update DISPATCH_SITES."
        )

    @pytest.mark.parametrize(
        "func,known", DISPATCH_SITES, ids=lambda v: getattr(v, "__name__", str(v))
    )
    def test_it_reads_no_leading_prefix_the_deny_list_lacks(self, func, known):
        undeclared = {
            prefix
            for prefix in _leading_prefixes(func)
            if not prefix.isalnum() and prefix not in _RESERVED_DISPATCH_SIGILS
        }
        assert not undeclared, (
            f"{func.__name__} dispatches on {sorted(undeclared)!r}, which "
            f"_RESERVED_DISPATCH_SIGILS does not declare -- a stripped "
            f"`(recommended) {sorted(undeclared)[0]}...` label would become a dispatch."
        )

    def test_the_deny_list_is_not_empty(self):
        assert _RESERVED_DISPATCH_SIGILS
