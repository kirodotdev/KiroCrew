"""has_active_turn() must read False on a spawned but never-prompted client.

`_turn_done` is an asyncio.Event whose CLEARED state means "a turn is in
flight". It is cleared at every prompt entry point and set again in the prompt
loop's finally. If it is left in its default (cleared) state at construction,
a client whose process is alive but which has never run a prompt reports
has_active_turn() == True — a false positive. The model/agent-switch endpoints
gate on has_active_turn(), so that false positive refuses a model switch on a
brand-new session with `turn_in_flight` (409) and never clears, because no real
turn exists to reach the finally that would set the event.

These are RED before the fix (event constructed cleared) and GREEN after
(event constructed set).
"""

from unittest.mock import MagicMock

from kiro_crew.acp.client import AcpClient


def _alive_never_prompted(tmp_path):
    """An AcpClient with a live process that has never run a prompt."""
    client = AcpClient(work_dir=tmp_path)
    proc = MagicMock()
    proc.returncode = None  # _is_process_alive() -> True
    client._process = proc
    return client


def test_idle_spawned_client_has_no_active_turn(tmp_path):
    """A spawned, never-prompted client is idle, not mid-turn."""
    client = _alive_never_prompted(tmp_path)
    assert client.has_active_turn() is False


def test_cleared_turn_done_still_reads_active(tmp_path):
    """The fix must not mask a real turn: once _turn_done is cleared (as every
    prompt entry point does before running), the client reads as active."""
    client = _alive_never_prompted(tmp_path)
    client._turn_done.clear()
    assert client.has_active_turn() is True


def _rearm_live_process(client):
    """_reset_state closes the process; give it a fresh live one so the assert
    turns on the event state, not on liveness."""
    proc = MagicMock()
    proc.returncode = None  # _is_process_alive() -> True after re-spawn
    client._process = proc


def test_reset_of_idle_client_stays_idle(tmp_path):
    """Resetting an IDLE client (event set) leaves it idle after re-spawn, so a
    later model switch is not refused with turn_in_flight."""
    client = _alive_never_prompted(tmp_path)  # _turn_done set from __init__
    client._reset_state()
    _rearm_live_process(client)
    assert client.has_active_turn() is False


def test_reset_midturn_stays_active(tmp_path):
    """Resetting on the respawn path runs mid-turn: a prompt entry point already
    cleared _turn_done, and the turn keeps running after the respawn, so the
    reset must preserve the cleared (active) state — not force it to idle, which
    would drop a concurrent cancel and mis-gate the live turn as switchable."""
    client = _alive_never_prompted(tmp_path)
    client._turn_done.clear()  # a turn is in flight when the reset fires
    client._reset_state()
    _rearm_live_process(client)
    assert client.has_active_turn() is True
