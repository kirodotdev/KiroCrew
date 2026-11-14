"""The ssh-to-self floor gives the same answer whatever the process's hash seed is.

A glob such as ``s*`` can name ssh, scp and sftp.  Where bash may run it, the floor
reads every later word as a host whatever program it expands to.  Where it is proven
another program's operand, and for a word of only ``*``, the floor reads it as the
first of them in ``_SSH_FAMILY_VERB_ORDER``, the same one in every process, so no set
order decides which program's operand grammar applies.  The rest of the floor's tests
live in ``test_denied_commands_security_ssh_self.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

# Run in a child so the hash seed is the test's own, not the runner's.  Every name
# lookup, reverse lookup and socket connect fails in the child, so it never leaves
# the machine; the rows name a literal loopback, which needs none of them.
_SEEDED_FLOOR_CHILD = """
import json, socket, sys
sys.path[:0] = json.loads(sys.argv[1])
def _no_network(*args, **kwargs):
    raise OSError("no network in this test")
socket.getaddrinfo = _no_network
socket.gethostbyaddr = _no_network
socket.gethostbyname = _no_network
socket.gethostbyname_ex = _no_network
socket.socket.connect = _no_network
from kiro_crew.security.argv_floor_ssh import _is_ssh_to_self, _ssh_family_verb
rows = json.loads(sys.argv[2])
print(json.dumps([_ssh_family_verb("s*"), [_is_ssh_to_self(row) for row in rows]]))
"""

# Rows whose answer followed the hash seed while a set chose the program: a glob that
# can name several programs before a copy destination, a lone star before a command
# that names a local port, and a cron line of stars before one.
_ROWS = [
    "s* ./payload.txt localhost:out",
    "s[sc]* ./payload.txt localhost:out",
    "*s* -a x localhost:/y",
    "git add *; curl localhost:3000/x",
    "0 * * * * curl localhost:8080/health",
    "s* localhost",
    "s* -b batch localhost",
]


def _floor_answers(seed: str) -> list:
    done = subprocess.run(
        [sys.executable, "-c", _SEEDED_FLOOR_CHILD, json.dumps(sys.path), json.dumps(_ROWS)],
        env={**os.environ, "PYTHONHASHSEED": seed},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,  # flake-ok: bound on the child, never a wait
        check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_floor_answers_the_same_under_two_hash_seeds():
    # A set iteration picks sftp for ``s*`` under seed 0 and scp under seed 3, so with
    # one the first five rows were allowed in one process and refused in another.
    first, second = _floor_answers("0"), _floor_answers("3")
    assert first == second
    # A glob naming several programs in program position is read as all of them,
    # so a copy to this host and a glob in ssh's host position are refused.  A
    # word of only ``*`` keeps scp's reading, whose cost is a word in a later
    # command that has a host:port shape, which it takes for a copy target.
    assert first == ["scp", [True, True, True, True, True, True, True]]
