"""The ssh-to-self floor matches a glob word in time linear in the word.

``fnmatch`` scans the rest of a pattern for a ``]`` at every ``[`` that never closes,
so a word made of a long run of ``[`` held the deny decision for seconds. The floor
matches through ``argv_floor_hosts._glob_matches``, which answers such a pattern
without ``fnmatch`` when the name holds no ``[``. Cost is asserted on interpreter
instruction counts, never a clock (testing-conventions, class 5).
"""

from __future__ import annotations

import fnmatch
from itertools import product

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _assert_scan_is_linear

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


@pytest.mark.parametrize(
    ("head", "sizes"),
    [
        pytest.param("echo ", (500, 1000, 1500), id="a-word-the-verb-gate-reads"),
        # The verb check and the host check both read the destination word.
        pytest.param("ssh ", (600, 1200, 1800), id="a-destination-the-ssh-walk-reads"),
    ],
)
def test_a_run_of_unclosed_brackets_keeps_is_denied_linear(head, sizes):
    # ``fnmatch`` caches each compiled pattern for the process, so the cases use
    # sizes no other case shares: a cached run would hide the quadratic compile.
    _assert_scan_is_linear(lambda n: head + "[" * n, sizes, denied=False)


def test_the_glob_matcher_answers_as_fnmatch():
    # Every pattern of up to five characters over ``s[]!*``, against names with and
    # without a ``[``: the names with one take the ``fnmatch`` path for an unclosed ``[``.
    from kiro_crew.security.argv_floor_hosts import _glob_matches

    names = ("ssh", "s", "s[", "[s]", "]!", "")
    for length in range(1, 6):
        for chars in product("s[]!*", repeat=length):
            pattern = "".join(chars)
            for name in names:
                expected = fnmatch.fnmatchcase(name, pattern)
                assert _glob_matches(name, pattern) is expected, (name, pattern)
