"""A child that owns a session ledger's writes and parks with a turn still open.

The cross-process half of write ownership cannot be asserted inside one process:
an advisory lock belongs to an open file description, so a second descriptor here
would contend, but no in-process fixture can produce the case that matters -- a
LIVE writer in another process whose turn is still producing entries while a
resume in this one decides whether to close it.

The handshake is over the pipes, not a clock: this child announces on stdout that
it owns the log and has an open turn, then BLOCKS reading stdin until the parent
tells it to exit. Nothing sleeps, and the parent's read of the announcement is
what orders the two processes.

Usage: ledger_lease_child.py <session-id>
"""

from __future__ import annotations

import sys

from kiro_crew import ledger as lg
from kiro_crew.ledger import Ledger

#: Written to stdout once the turn is open and this process owns the log.
OWNED = "owned"
#: Written to stdin by the parent to release the child.
RELEASE = "release"


def main() -> int:
    session_id = sys.argv[1]
    Ledger.create(lg.KIND_SESSION, session_id, owner="qa", agent="kirocrew")
    handle = Ledger.open(lg.KIND_SESSION, session_id)
    # Two entries the parent can recognize: a turn that is open, and a tool call
    # inside it that has no outcome yet. A repair would close both.
    handle.append("turn/started", {"turn": 1, "actor": "user"}, src="acp")
    handle.append("tool/called", {"turn": 1, "call_id": "tc-1", "name": "fs_write"}, src="acp")

    sys.stdout.write(f"{OWNED}\n")
    sys.stdout.flush()
    # Blocks until the parent writes a line or closes the pipe. The handle stays
    # referenced for the whole wait, so ownership is held for it.
    sys.stdin.readline()
    assert handle.last_seq >= 2, "the child lost its own handle"
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
