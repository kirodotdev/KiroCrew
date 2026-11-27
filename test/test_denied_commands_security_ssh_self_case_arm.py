"""A ``case`` arm's pattern words are never an ssh verb in ``sandbox-escape-ssh-self``.

A pattern such as ``*)`` sits where a program would, and its glob can name an
ssh-family program, but the shell only matches it against the case word. Read as
a verb, the word after it (``exit`` in ``*) exit 0 ;;``) became a host. The verdict
on that host then depends on whether this machine's DNS resolves the name, so the
same hook script was refused on one host and allowed on another. These tests judge
every host as one this machine cannot classify, the worst case such a host meets.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_RULE = "sandbox-escape-ssh-self"

# A PreToolUse hook script in the shape a user's agent spec carries: a ``case``
# on the tool name whose catch-all arm exits.
_HOOK_SCRIPT = """#!/bin/bash
EVENT=$(cat)
TOOL=$(echo "$EVENT" | jq -r '.tool_name')

case "$TOOL" in
  @ops-mcp/*) ;;
  *) exit 0 ;;
esac

API=$(echo "$TOOL" | sed 's|@ops-mcp/||')
if echo "GetThing ListThings" | grep -qw "$API"; then
  exit 0
fi
echo "BLOCKED: $API needs approval" >&2
exit 2
"""


@pytest.fixture(autouse=True)
def _every_host_unclassified(request, _floor_monkeypatch):
    own_mp = pytest.MonkeyPatch()
    request.addfinalizer(own_mp.undo)
    own = security.socket.gethostname().strip().lower()
    pinned = frozenset(name for name in {own, own.split(".", 1)[0]} if name)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_NAMES_CACHE", pinned)
    _floor_monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
    _floor_monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    own_mp.setattr(_argv_floor, "_OWN_HOST_RESOLVE_NEXT_TRY", float("inf"))
    # No DNS: every name the floor asks about reads as one that did not resolve.
    _floor_monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", lambda host, **_kw: True)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_HOOK_SCRIPT, id="hook-script"),
        pytest.param('case "$t" in\n  *) exit 0 ;;\nesac', id="star-arm-multiline"),
        pytest.param('case "$t" in *) exit 0 ;; esac', id="star-arm"),
        pytest.param('case "$t" in (*) exit 0 ;; esac', id="paren-star-arm"),
        pytest.param('case "$t" in a) true ;; *) exit 1 ;; esac', id="second-star-arm"),
        pytest.param(
            'case "$t" in ssh|scp) echo remote ;; *) exit 0 ;; esac', id="literal-ssh-pattern"
        ),
        pytest.param('case "$t" in s*) exit 0 ;; esac', id="s-star-arm"),
        pytest.param('case "$t" in a) true ;; esac>/dev/null', id="redirected-esac-no-ssh"),
        pytest.param('case "$t" in a|b) true ;; *) exit 0 ;; esac', id="alternation"),
        pytest.param('case "$t" in a | b) true ;; *) exit 0 ;; esac', id="spaced-alternation"),
        pytest.param('case "$t" in a) true ;; *) exit 0\nesac', id="last-arm-no-double-semi"),
        pytest.param("x=1; case x in *) exit 0 ;; esac", id="case-after-command"),
    ],
)
def test_a_case_pattern_is_not_an_ssh_verb(text):
    assert _denied_by(text) is None, text


@pytest.mark.parametrize(
    "text",
    [
        'case "$t" in *) ssh localhost ;; esac',
        'case "$t" in\n  *) ssh localhost id ;;\nesac',
        'case "$t" in a) true ;; *) ssh 127.0.0.1 ;; esac',
        'case "$t" in *) s?h localhost ;; esac',
        'case "$t" in *) true ;; esac; ssh localhost',
        'case "$t" in *) exit 0 ;; esac\nssh localhost',
        "echo case in x; ssh localhost",
        "ssh localhost",
        # The floor still reads an unclassified host after a real verb.
        'case "$t" in *) ssh exit ;; esac',
        # A function named ``CASE`` is not a case: no pattern word follows it.
        pytest.param("CASE(){ :; }; CASE x in ; ssh -p 22 localhost", id="case-function-name"),
        # An arm body glued to the ``)`` still runs its program.
        pytest.param('case "$t" in x)/usr/bin/ssh localhost ;; esac', id="glued-body-path"),
        pytest.param('case "$t" in *)/usr/bin/s?h localhost ;; esac', id="glued-body-glob"),
        pytest.param('case "$t" in x)ssh 127.0.0.1 ;; esac', id="glued-body-bare"),
        # An ``esac`` glued to an operator or redirect still ends the case.
        pytest.param('case "$t" in a) true;; esac;ssh localhost', id="esac-semicolon"),
        pytest.param('case "$t" in a) true;; esac&&ssh localhost', id="esac-and"),
        pytest.param('case "$t" in a) true;; esac>/dev/null; ssh localhost', id="esac-redirect"),
        pytest.param('case "$t" in a) true;; esac2>&1; ssh 127.0.0.1', id="esac-fd-redirect"),
        pytest.param(
            'case "$t" in a) true;; esac&>/dev/null; ssh localhost', id="esac-amp-redirect"
        ),
        pytest.param('case "$t" in a) true;; esac|ssh localhost', id="esac-pipe"),
        # A copied ssh binary whose name ends in ``)`` is a program, not a pattern.
        pytest.param(
            "cp /usr/bin/ssh '/tmp/s)'; case \"$t\" in a) true;; esac>/dev/null; '/tmp/s)' localhost",
            id="copied-binary-after-redirect-esac",
        ),
        pytest.param(
            "cp /usr/bin/ssh '/tmp/s)'; case \"$t\" in a) '/tmp/s)' 127.0.0.1 ;; esac",
            id="copied-binary-arm-body",
        ),
        pytest.param(
            'cp /usr/bin/ssh /tmp/s; case "$t" in a) true;; esac<in; /tmp/s localhost',
            id="copied-binary-after-input-redirect-esac",
        ),
        pytest.param('case "$t" in a) true ;; esac\nssh 127.0.0.1', id="ssh-after-bare-esac"),
        # A case that is only one in its lowered or dequoted spelling is no case.
        pytest.param(
            "cp /usr/bin/ssh '/tmp/s)'; CASE x in ; '/tmp/s)' 127.0.0.1\n;; esac",
            id="uppercase-case",
        ),
        pytest.param('"case" x in a) true;; esac; ssh localhost', id="double-quoted-case"),
        pytest.param("'case' x in a) true;; esac; ssh localhost", id="single-quoted-case"),
        pytest.param("\\case x in a) true;; esac; ssh localhost", id="escaped-case"),
        pytest.param('case "$t" "in" a) true;; esac; ssh localhost', id="quoted-in"),
        pytest.param('case x in a) true;; "esac"; ssh localhost', id="quoted-esac"),
        pytest.param("case x in a) true;; esac>/dev/null; ssh localhost", id="esac-redirect-x"),
        pytest.param("case x in a) true;; esac&&ssh localhost", id="esac-and-x"),
        pytest.param("case x in a) true;; esac|ssh localhost", id="esac-pipe-x"),
        pytest.param("case x in a) true;; esac>f; ssh localhost", id="esac-redirect-file"),
        pytest.param(
            "cp /usr/bin/ssh '/tmp/s)'; CASE x in ; '/tmp/s)' 127.0.0.1 ;; esac",
            id="copied-binary-uppercase-case",
        ),
        pytest.param(
            "cp /usr/bin/ssh '/tmp/s)'; case x in a) true;; esac>/dev/null; '/tmp/s)' 127.0.0.1",
            id="copied-binary-after-redirect-esac-x",
        ),
        pytest.param('case x in "a)" ssh localhost ;; esac', id="quoted-pattern"),
        pytest.param("case x in *) /usr/bin/s?h localhost ;; esac", id="glob-verb-in-body"),
    ],
)
def test_an_ssh_the_shell_runs_is_still_refused(text):
    assert _denied_by(text) == _RULE, text
