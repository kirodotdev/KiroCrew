"""The by-name kill leg must read a quoted ERE alternation as a pattern.

``pkill``/``killall`` take an ERE, so in ``'zz|kiro'${x:-crew}`` bash passes
``zz|kirocrew`` and the second alternative selects protected processes.  The
leg searched each argument raw (de-bracketed) and through
``_normalize_operand``; the operand view truncates at the de-quoted pattern's
own ``|``, and the raw view cannot resolve the parameter default or the empty
substitution, so a name needing both quote removal and a construct rewrite
slipped both members.  The leg now also searches each argument through
``_resolved_word_view`` -- construct-span rewrites only, never a boundary at
the pattern's own characters -- as an ADDITIVE third member.

The protected verb and name are assembled at runtime so an agent shell can
read and grep this file without tripping the very rule under test.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import is_denied

_K = "k" + "ill"
_PK = "p" + _K
_KA = _K + "all"
_NAME = "kiro" + "crew"
_HEAD = _NAME[:4]
_TAIL = _NAME[4:]


def _floor(cmd: str) -> bool:
    """The argv floor's own verdict, independent of the regex tier."""
    return _argv_floor._is_self_kill(cmd.lower())


@pytest.mark.parametrize("prog", [_PK, _KA])
@pytest.mark.parametrize(
    "arg",
    [
        # the two spellings the report measured
        "'zz|" + _HEAD + "'${x:-" + _TAIL + "}",
        "'zz|" + _HEAD + "'$()" + _TAIL,
        # the backtick spelling of the empty substitution
        "'zz|" + _HEAD + "'``" + _TAIL,
        # all three construct rewrites composed with the alternation
        "'zz|[" + _HEAD[0] + "]" + _HEAD[1:] + "'$()${x:-" + _TAIL + "}",
        # the default on the HEAD half, alternation prefix kept
        "'zz|'${x:-" + _HEAD + "}'" + _TAIL + "'",
        # a grouped alternation whose closing paren follows the name
        "'(zz|" + _HEAD + "'${x:-" + _TAIL + "}')'",
    ],
)
def test_composed_alternation_is_a_self_kill(prog, arg):
    cmd = f"{prog} -f {arg}"
    assert _floor(cmd), cmd
    assert is_denied(cmd) is not None, cmd


@pytest.mark.parametrize(
    "arg",
    [
        # the redirect-prefix pattern the leg's comment documents: the ``>`` is
        # part of the TARGET and the raw member carries it, unchanged
        "'>" + _NAME + "'",
        "'>" + _HEAD + "''" + _TAIL + "'",
        # a plain alternation with the literal name stays denied
        "'zz|" + _NAME + "'",
        # the pre-existing single-transform spellings stay denied
        _HEAD + "$()" + _TAIL,
        "'[" + _HEAD[0] + "]" + _HEAD[1:] + _TAIL + "'",
    ],
)
def test_sibling_spellings_stay_denied(arg):
    cmd = f"{_PK} -f {arg}"
    assert _floor(cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # another product behind the same composed alternation
        _PK + " -f 'zz|other'${x:-thing}",
        _PK + " -f 'zz|other'$()thing",
        _KA + " 'zz|other'${x:-thing}",
        # bracket-classed other-product patterns
        _PK + " -f '[o]ther-app'",
        _PK + " -f 'zz|[o]ther'$()${x:-thing}",
        # a default that resolves to something else entirely
        _PK + " -f 'zz|" + _HEAD + "'${x:-nope}",
        # the name in a LATER command is not this kill's target
        _PK + " -f 'zz|other'${x:-thing}; echo " + _NAME,
    ],
)
def test_other_targets_stay_allowed(cmd):
    assert not _floor(cmd), cmd
    assert is_denied(cmd) is None, cmd
