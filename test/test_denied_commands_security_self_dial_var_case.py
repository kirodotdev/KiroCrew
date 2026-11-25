"""The self-dial floors match a same-line shell variable name case-sensitively.

Bash variable names are case-sensitive, so `H` and `h` are two variables. The
ssh-to-self and git/tar-to-self floors read a command lowercased, which once let
a command assign this host to `H` and a remote to a later `h`, so the floor
resolved the remote and allowed the command while bash dialled this host. The
floors now resolve a same-line assignment by the name's exact case.

Every probe calls `is_denied` only; no candidate command is executed.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import (  # noqa: F401
    _denied_by,
    _own_address_probe_stays_local,
)

from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import is_denied

_S = "local" + "host"
_GIT_TAR = "sandbox-escape-git-tar-self"
_SSH = "sandbox-escape-ssh-self"


@pytest.fixture(autouse=True)
def _dotted_hosts_resolve_remote(_floor_monkeypatch):
    """No destination DNS in this module: a dotted host gets a fixed REMOTE
    verdict (the stub the sibling self-floor modules use), so the loopback
    literals are the only self and both readings agree run to run."""
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: False)


@pytest.mark.parametrize(
    ("cmd", "rule"),
    [
        # Self in the upper name, a remote in its lower twin assigned later: the
        # upper name resolves itself, so the self-dial is refused.
        (f"H={_S}; h=github.com; git clone $H:x", _GIT_TAR),
        (f"H={_S}; h=github.com; tar -xf $H:/x", _GIT_TAR),
        (f"H={_S}; h=github.com; ssh $H true", _SSH),
        # The reverse twin, and the remote assigned first.
        (f"h={_S}; H=github.com; git clone $h:x", _GIT_TAR),
        (f"github=x; GITHUB={_S}; git clone ${{GITHUB}}:x", _GIT_TAR),
    ],
    ids=["git-H", "tar-H", "ssh-H", "git-h", "braced"],
)
def test_a_self_dial_hidden_in_a_case_twin_is_refused(cmd, rule):
    assert _denied_by(cmd) == rule, cmd


# The same twin in the braced ``${H}`` and double-quoted ``"$H"`` / ``"${H}"``
# reference spellings, remote assigned last to the lower twin. The resolver reads
# these references, so the flat gate accepts them and the exact pass refuses the
# self-dial. (github.com resolves remote via the module's resolver stub.)
@pytest.mark.parametrize(
    ("cmd", "rule"),
    [
        (f"GITHUB={_S}; github=github.com; git clone ${{GITHUB}}:x", _GIT_TAR),
        (f"H={_S}; h=github.com; git clone ${{H}}:x", _GIT_TAR),
        (f'H={_S}; h=github.com; ssh "$H" true', _SSH),
        (f'H={_S}; h=github.com; ssh "${{H}}" true', _SSH),
    ],
    ids=["braced-git", "braced-git-h", "quoted-ssh", "quoted-braced-ssh"],
)
def test_a_braced_or_quoted_case_twin_is_refused(cmd, rule):
    assert _denied_by(cmd) == rule, cmd


# A genuine remote in the same braced/quoted spellings stays allowed.
@pytest.mark.parametrize(
    "cmd",
    [
        f"h={_S}; H=github.com; git clone ${{H}}:x",
        f'h={_S}; H=github.com; ssh "$H" true',
    ],
    ids=["braced", "quoted"],
)
def test_a_braced_or_quoted_genuine_remote_stays_allowed(cmd):
    assert is_denied(cmd) is None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        # Same variable, reassigned to a real remote: bash uses the last value, and
        # so does the floor now that the names match by case. Allowed, as before.
        f"h={_S}; h=github.com; git clone $h:x",
        "H=github.com; git clone $H:x",
        f"h={_S}; H=github.com; git clone $H:x",
        "git clone github.com:x",
    ],
    ids=["reassigned-remote", "upper-remote", "twin-remote-used", "plain-remote"],
)
def test_a_genuine_remote_stays_allowed(cmd):
    assert is_denied(cmd) is None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        f"h={_S}; git clone $h:x",
        f"H={_S}; git clone $H:x",
        f"a={_S}; git clone $a:x",
        f"a={_S}; ssh $a true",
    ],
    ids=["lower", "upper", "single-git", "single-ssh"],
)
def test_a_single_variable_self_dial_stays_refused(cmd, rule=None):
    assert is_denied(cmd) is not None, cmd


# On a line a source transform rewrites, or a nested carried line, the submitted
# case does not align, so the resolver matches the name lowered. That shape reads
# as the base floor does, so an all-lowercase reassignment to a real remote stays
# allowed.
@pytest.mark.parametrize(
    "cmd",
    [
        "h=github.com; h=git.example.org; : $(); git clone $h:x",
        "a=github.com; A=example.net; git clone $a:x",
    ],
    ids=["reassign-rewritten", "twin-lower-used"],
)
def test_a_rewritten_line_keeps_mains_reading(cmd):
    assert is_denied(cmd) is None, cmd


# Exact-case resolution runs only on a simple flat line. A line that carries a
# conditional, a group, or a name rebinding is not flat, so the floors read it
# exactly as before this change. Each line below dials this host, which main
# already refuses; the point is that leaving the line to main's reading never
# turns that refusal into an allow.
@pytest.mark.parametrize(
    "cmd",
    [
        f"H={_S}; false && H=github.com; h={_S}; git clone $H:x",
        f"H={_S}; (H=github.com); h={_S}; git clone $H:x",
        f"H=h; h={_S}; declare -n H; git clone $H:x",
    ],
    ids=["conditional", "group", "rebind"],
)
def test_a_non_flat_line_keeps_its_refusal(cmd):
    assert is_denied(cmd) is not None, cmd


# A segment that is not pure assignment words is not flat: a name=value that is
# an argument to a command (`echo H=github.com`), or a prefix assignment scoped
# to one command (`H=github.com true`), is not an assignment bash keeps for the
# rest of the line. Each line below dials this host, which the base floor already
# refuses; exact-case resolution must not read the scoped/argument value and turn
# that refusal into an allow.
@pytest.mark.parametrize(
    "cmd",
    [
        f"H={_S}; echo H=github.com; h={_S}; git clone $H:x",
        f"H={_S}; H=github.com true; h={_S}; git clone $H:x",
        f"H={_S}; H=github.com true; h={_S}; ssh $H true",
    ],
    ids=["argument", "prefix-git", "prefix-ssh"],
)
def test_a_segment_that_is_not_pure_assignment_keeps_its_refusal(cmd):
    assert is_denied(cmd) is not None, cmd


# Only the last segment may be a command, and no value is read from it. A builtin
# that assigns a variable with no name=value word (`printf -v H v`, `read H`), and
# a quoted or commented separator that bash never splits on, both reach this code
# before quoted separators are masked. Each line below dials this host, which the
# base floor already refuses; exact-case resolution must leave it refused.
@pytest.mark.parametrize(
    "cmd",
    [
        f"H=github.com; h={_S}; printf -v H {_S}; git clone $H:x",
        f"H=github.com; printf -v H {_S}; h={_S}; git clone $H:x",
        f"H=github.com; printf -v H {_S}; h={_S}; ssh $H true",
        f'H={_S}; echo "a; H=github.com ;b"; h={_S}; git clone $H:x',
    ],
    ids=["builtin-last", "builtin-mid", "builtin-ssh", "quoted-separator"],
)
def test_a_multi_command_or_builtin_assignment_keeps_its_refusal(cmd):
    assert is_denied(cmd) is not None, cmd


# bash blanks are space, tab and newline only. A separator bash does not split on
# (CR, NBSP, VT, FF, an em space, a unit separator) must not let the floor read a
# phantom assignment. Each line dials this host, which the base floor refuses.
@pytest.mark.parametrize(
    "ws",
    ["\r", "\u00a0", "\x0b", "\x0c", "\u2003", "\x1f"],
    ids=["cr", "nbsp", "vtab", "formfeed", "em-space", "unit-sep"],
)
def test_a_non_bash_whitespace_separator_keeps_its_refusal(ws):
    assert is_denied(f"H={_S}; x=1{ws}H=github.com; h={_S}; git clone $H:x") is not None
    assert is_denied(f"H={_S}; x=1{ws}H=github.com; h={_S}; ssh $H true") is not None


# An IFS reassignment changes word splitting, so a value the resolver reads as one
# token can be two words to bash. The line keeps the lowered read and stays refused.
@pytest.mark.parametrize(
    "cmd",
    [
        f"IFS=/; H={_S}/id; h={_S}; git clone $H:x",
        f"IFS=/; H={_S}/id; h={_S}; ssh $H true",
    ],
    ids=["git", "ssh"],
)
def test_an_ifs_reassignment_keeps_its_refusal(cmd):
    assert is_denied(cmd) is not None, cmd


# A special bash variable has non-literal assignment/read semantics, so the
# value the resolver reads is not the token bash uses (`OPTIND=remotehost` makes
# bash expand `$OPTIND` to `0`). The exact-case read must not turn the base
# floor's refusal into an allow. One row per representative name.
@pytest.mark.parametrize(
    "name",
    ["OPTIND", "SECONDS", "HISTCMD", "LINENO", "RANDOM", "EUID"],
)
def test_a_special_variable_assignment_keeps_its_refusal(name):
    cmd = f"{name}=remotehost; {name.lower()}={_S}; git clone ${name}:x"
    assert is_denied(cmd) is not None, cmd


# A value the assignment parser cannot read (one with `+`, `,` or `%`) must not
# let the exact-case read resolve a reference to a stale earlier value. The line
# dials this host, which the base floor refuses.
@pytest.mark.parametrize(
    "cmd",
    [
        f"H=github.com:x; H={_S}:x+y; h={_S}:x; git clone $H",
        f"H=github.com:x; H={_S}:x,y; h={_S}:x; git clone $H",
        f"H=github.com:x; H={_S}:x%y; h={_S}:x; ssh $H",
    ],
    ids=["plus", "comma", "percent"],
)
def test_a_value_the_parser_cannot_read_keeps_its_refusal(cmd):
    assert is_denied(cmd) is not None, cmd


# A reference can concatenate with literal text (`${H}host`), so a write bash
# makes but the resolver cannot see (`printf -v`, `read`) must not let the
# exact-case value replace the lowered value and break the concatenation that
# forms this host. The union (deny if the lowered OR the exact read dials self)
# keeps the refusal here.
@pytest.mark.parametrize(
    "cmd",
    [
        "H=localhost; printf -v H local; h=local; ssh ${H}host true",
        "H=localhost; printf -v H local; h=local; git clone ${H}host:x",
        "H=localhost; read H <<< local; h=local; ssh ${H}host true",
    ],
    ids=["printf-ssh", "printf-git", "read-ssh"],
)
def test_a_concatenated_reference_keeps_mains_refusal(cmd):
    assert is_denied(cmd) is not None, cmd


# A decoy assignment word -- a name=value argument (`echo H=v`), a prefix
# assignment (`H=v cmd`), a builtin that assigns (`printf -v H v`), or a value the
# parser cannot read -- makes the line not a flat simple sequence, so the
# exact-case reading is skipped and the line reads exactly as main: a genuine
# remote there is allowed, not over-refused. (github.com resolves remote via the
# module's resolver stub.)
@pytest.mark.parametrize(
    "cmd",
    [
        "H=github.com; echo H=localhost; h=github.com; ssh $H true",
        "H=localhost; printf -v H github.com; h=github.com; ssh $H true",
        "H=localhost:x; H=github.com:x+y; h=github.com:x; git clone $H",
        "H=localhost:/x; H=github.com:/x+y; h=github.com:/x; tar -xf $H",
    ],
    ids=["echo-arg", "printf-v", "plus-value-git", "plus-value-tar"],
)
def test_a_decoy_assignment_word_reads_as_main(cmd):
    assert is_denied(cmd) is None, cmd


def test_head_deny_set_is_a_superset_of_mains():
    """Property: the submitted-case read may only ADD a refusal, never remove one.
    So for any line, if the lowered (main) read dials self, the submitted-case
    read dials self too. Seeded, in-process, no I/O, no leaks.
    """
    import random

    from kiro_crew.security.argv_floor_ssh import _is_ssh_to_self

    rng = random.Random(0xC0FFEE)
    names = ["h", "H", "a", "B", "OPTIND", "optind", "IFS", "x", "Y"]
    # Only values the floor decides WITHOUT a name lookup: the two loopback
    # literals (self) and one stable remote the suite uses everywhere. A dotted
    # random token would hit real DNS and make the verdict nondeterministic.
    hosts = ["localhost", "127.0.0.1", "github.com"]

    for _ in range(2000):
        segs = []
        for _ in range(rng.randint(1, 4)):
            nm = rng.choice(names)
            val = rng.choice(hosts)
            segs.append(f"{nm}={val}")
        ref = rng.choice([s.split("=", 1)[0] for s in segs])
        cased = "; ".join(segs) + f"; ssh ${ref}"
        text = cased.lower()
        main_deny = _is_ssh_to_self(text, raw_text=None)
        head_deny = _is_ssh_to_self(text, raw_text=cased)
        assert not main_deny or head_deny, cased


def test_case_matching_in_the_resolver():
    from kiro_crew.security.argv_floor_ssh import _resolve_line_assignments

    # With the submitted case, `$H` resolves `H`, not the lower `h` after it.
    assert (
        _resolve_line_assignments(f"h={_S}; h=github.com; $h", cased=f"H={_S}; h=github.com; $H")
        == f"h={_S}; h=github.com; {_S}"
    )
    # Without aligned cased text, the name falls back to the lowered match.
    assert _resolve_line_assignments("a=x; $a") == "a=x; x"
    # Misaligned length: fall back, do not index out of range.
    assert _resolve_line_assignments("a=x; $a", cased="A=x") == "a=x; x"
