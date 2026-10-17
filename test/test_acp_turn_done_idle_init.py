"""An idle AcpClient must not read as "a turn is running".

``has_active_turn()`` is the 409 ``turn_in_flight`` gate on set-model /
set-agent. It reads ``not _turn_done.is_set() and _is_process_alive()``. An
``asyncio.Event`` starts UNSET, so a client whose process is alive but that
has never been prompted (eager spawn on a fresh chat) reported a turn in
flight and the user could not switch model until a real turn's ``finally``
set the flag. Idle must read as done; every prompt entry ``clear()``s the
Event before sending, so a real turn still reads active.

``_reset_state()`` must leave the Event alone. The prompt entries clear it
BEFORE ``ensure_ready()``, and ``ensure_ready()`` reaches ``_reset_state()`` on
the dead-process respawn branch. Rebuilding the Event there would mark the
whole respawned turn as done, and the shutdown drain (``has_unfinished_turn``)
would then kill the replacement process mid-turn.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.acp.client import AcpClient


def _live_process() -> MagicMock:
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = None  # _is_process_alive() -> True
    return proc


def _dead_process() -> MagicMock:
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = 1
    return proc


def test_fresh_client_reads_idle_even_with_live_process() -> None:
    client = AcpClient()
    client._process = _live_process()

    assert client._turn_done.is_set()
    assert client.has_active_turn() is False
    assert client.has_unfinished_turn() is False


def test_prompt_start_still_reads_active() -> None:
    # The prompt entries clear() the Event before sending; the idle default
    # must not mask that.
    client = AcpClient()
    client._process = _live_process()

    client._turn_done.clear()

    assert client.has_active_turn() is True


def test_reset_state_keeps_idle_when_idle() -> None:
    client = AcpClient()
    client._process = _dead_process()

    client._reset_state()  # e.g. a failed startup outside any turn
    client._process = _live_process()  # re-spawn

    assert client._turn_done.is_set()
    assert client.has_active_turn() is False


def test_reset_state_keeps_turn_in_flight_across_respawn() -> None:
    # send_message()/stream_events(): clear() -> ensure_ready(). With the
    # previous process dead, ensure_ready() runs _reset_state() and re-spawns.
    # The turn is still in flight after that, so the shutdown drain must still
    # see it.
    client = AcpClient()
    client._process = _dead_process()
    client._turn_done.clear()  # prompt entry, turn begins
    event_before = client._turn_done

    client._reset_state()  # respawn branch inside ensure_ready()
    client._process = _live_process()

    assert client._turn_done is event_before  # waiters are not orphaned
    assert client.has_active_turn() is True
    assert client.has_unfinished_turn() is True
