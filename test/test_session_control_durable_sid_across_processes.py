"""The id recorded for another slot survives a real process boundary.

Every other test of this resolution runs in one interpreter, where the session map is
in memory and answers for as long as that process lives -- so a resolver reading only
the map passes them all. The gap it is there to close opens at a RESTART: the slot whose
log a takeover names can be one this process has never served a turn for, and the map is
then either silent or naming a generation the slot has left behind.

So the store is written here and read THERE, in an interpreter that has mapped nothing.
The subprocess is the assertion; this module only sets up a store and reports what came
back. It is one test because the boundary is the expensive part, and it carries its own
control: the same child also reports what the session map answers, so a pass cannot be
explained by the map having been consulted.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from kiro_crew.crew_log import emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: Run in a FRESH interpreter against the store this module wrote. Drives the RESOLVER
#: rather than the store reader underneath it: that reader is shared code this change
#: only calls, so asserting on it alone would pass with this change reverted. Reports
#: the resolver's answer per slot and, as the control, what the session map holds --
#: which in a process that has served no turn is nothing at all.
#:
#: ``sessions`` stands in for the live registry, which genuinely does not exist here: no
#: session is running, so nothing owes replay. Its mapping half is the REAL
#: ``SessionMap``, reading the real file, because that is the source under test.
_CHILD = """
import asyncio, json, sys
from kiro_crew.dashboard import session_control as sc
from kiro_crew.session_map import SessionMap

mapped = SessionMap()


class _Sessions:
    _session_map = mapped

    @staticmethod
    def provider_switch_replay_pending(key):
        return False


class _State:
    sessions = _Sessions()
    _slots = {}


state = _State()
print(json.dumps({
    "resolved": {
        slot: asyncio.run(sc._recorded_sid_of(state, slot)) for slot in sys.argv[1:]
    },
    "mapped": {slot: mapped.mapped_sid("dashboard:" + slot) for slot in sys.argv[1:]},
}))
"""


@pytest.fixture
def _store(tmp_path, monkeypatch):
    """A real crew log store on disk, with no write armed on the shared pool."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    yield tmp_path / "home"
    stp.reset_for_tests()


def test_the_store_names_another_slots_log_in_a_process_that_mapped_nothing(_store, monkeypatch):
    """A restart's gateway can still say which log a slot is writing.

    Two slots, each with a succession: the older log and the one that replaced it. The
    child interpreter has no mapping for either -- it has served no turn -- so the only
    source left is the store, and it must name the CURRENT log of each, not the one it
    replaced.
    """
    for sid, slot, previous in (
        ("sid-boss-1", "chat-boss", ""),
        ("sid-boss-2", "chat-boss", "sid-boss-1"),
        ("sid-worker-1", "chat-worker", ""),
        ("sid-worker-2", "chat-worker", "sid-worker-1"),
    ):
        emit.on_session_opened(
            sid,
            agent="kirocrew",
            slot=slot,
            model="opus",
            cwd="/w",
            owner="raymond",
            previous_sid=previous,
        )
    assert emit.flush(timeout=10.0) is True

    child = subprocess.run(
        [sys.executable, "-c", _CHILD, "chat-boss", "chat-worker", "chat-never-existed"],
        capture_output=True,
        timeout=180,
        cwd=str(_store.parent),
        check=False,
        **UTF8_TEXT,
    )
    assert child.returncode == 0, child.stderr
    answer = json.loads(child.stdout.strip().splitlines()[-1])

    # Each slot's current log, across the boundary, resolved with no mapping to help.
    assert answer["resolved"]["chat-boss"] == "sid-boss-2"
    assert answer["resolved"]["chat-worker"] == "sid-worker-2"
    # A slot that never wrote is the determination "no log" -- an empty string, not the
    # undetermined answer, because the store was read whole.
    assert answer["resolved"]["chat-never-existed"] == ""
    # The control: the map answered nothing for any of them, so the three assertions
    # above cannot be explained by the process-local source the fix stopped relying on.
    assert set(answer["mapped"].values()) == {""}
