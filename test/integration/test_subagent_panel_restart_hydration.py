"""Restart recovery of the Subagents panel is seeded by the real slot-restore path.

The Subagents panel rebuilds from persisted run folders on a replaced gateway.
Every admission path on that rebuild requires ``state.get_slot(slot_key)`` to
answer non-None -- a slot that does not exist denies, by design, fail-closed. So
recovery depends on the slot being live: a persisted run reappears only if its
slot has been restored. The unit tests covering the panel
(``test_subagent_panel_durable_replay.py``) hand-register their slots -- they
verify the frames and the records, but the slot never arrives through the
production seeding path, so neither the restore step nor the fail-closed gate is
exercised against a slot that a real boot rebuilt.

This closes that gap the only way a unit test cannot: a real gateway RESTART
(a second boot on the same home) whose slot arrives through the production
seeding path -- ``restore_open_slots`` rehydrating ``open_slots.json`` during
boot -- rather than a hand-registered one. The test pins two things:

* ``restore_open_slots`` rebuilds the slot on a replacement boot, so a persisted
  run owned by that slot replays over the reconnect; and
* a fail-closed slot gate withholds a persisted run whose owning slot was never
  seeded.

Which gate fires, named precisely because the control's value is that it fires:
on the reconnect replay the folder reader runs TWO slot gates in series. The
pre-cap bound (``persisted_precap_denial_reason``) is reached first, and for a
dashboard owner its visibility half admits only slots that are live
(``visible_subagent_slot_keys`` returns every live slot key); a slot that was
never restored is absent from ``_slots``, so the pre-cap denies it
``persisted_not_visible`` before the record is ever folded. The authoritative
``persisted_replay_denial_reason`` -- whose own refusal for an unrestored slot
is ``slot_missing`` -- is the belt-and-braces check behind it, reached only by
records the pre-cap admitted. So in THIS path the gate that actually withholds
an unseeded run is the visibility pre-cap, with ``slot_missing`` standing behind
it. The control was confirmed able to fail against this seam: stubbing the
pre-cap's ``persisted_not_visible`` and the authoritative ``slot_missing`` both
open makes the control go red (the run replays), and either left in place keeps
it green -- so a regression in the slot gating is observed, not papered over.

The integration layer is the right home for it: ``gateway_boot`` boots the real
``GatewayOrchestrator.run()`` in this process, ``restart()`` is a genuine second
boot on the SAME ``KIROCREW_HOME`` with every home-bound global reset between
them exactly as a new process starts without them, and the live
``DashboardState`` is reachable for a direct assertion that the slot really was
seeded. The subprocess E2E route proves the same thing across a process
boundary; this proves it a step cheaper and asserts the seam the unit tests
cannot -- ``get_slot`` non-None at replay time, produced by seeding and not by a
test hand-registering it.

The shape:

1. plant a dashboard slot's transcript on the home (one metadata line, one
   message) -- the production seeding input ``restore_open_slots`` reads;
2. plant that slot's key in ``open_slots.json`` and a persisted subagent run
   folder owned by that slot's session (the ``subagents/<id>/`` tree a finished
   persistent run leaves behind, the same shape
   ``test_subagent_panel_durable_replay`` writes);
3. ``restart()`` -- a genuine second boot on the same home, which does not
   re-seed, so those two files are the replacement boot's recovery input;
4. assert ``get_slot`` is non-None on the new boot (seeding ran), then subscribe
   the Subagents panel over the owner WebSocket and read until the persisted
   run's ``subagent_done`` frame arrives.

The negative control plants the same record under a slot with no
``open_slots.json`` entry: nothing restores it, ``get_slot`` answers None, it is
absent from ``_slots``, and the fail-closed slot gate withholds the frame (the
visibility pre-cap denies it ``persisted_not_visible`` first, with the
authoritative ``slot_missing`` behind that). A second run owned by the real
seeded slot is planted as a replay-completion beacon, so the control reads until
the replay is observably finished before asserting the unseeded run is absent --
the withholding is proven, never an idle socket mistaken for an empty panel.

Both tests turn ``KIROCREW_CREW_LOG`` off so the replay's durable source is the
run-folder reader, which enumerates every run folder in the registry and then
runs the per-record slot gates. The crew-log fold is instead keyed off the LIVE
slot names, so an unseeded run would never be a candidate and the slot gate
would never run against it -- the control would pass without exercising the
thing it guards. The folder reader is also mtime-ordered newest-first, so the
beacon is planted older than the audited run: the reader offers the audited run
to the gate before it reaches the beacon's completion frame, so a gate that
leaked the audited run is observed rather than skipped past.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import aiohttp
import pytest
from integration.conftest import booted_gateway

from kiro_crew.history import _safe_key
from kiro_crew.subagent_persistence import _subagents_dir

_PLANTED_RUN_ID = "restart-hydration-run"
_BEACON_RUN_ID = "restart-hydration-beacon"
_PLANTED_TASK = "audit the restart recovery order"
_PLANTED_AGENT = "kirocrew-worker"
# A dashboard slot the test owns end to end. Its transcript stem is
# ``dashboard_<slot>`` and its session key is ``dashboard:<slot>``; the planted
# run is owned by that session, and ``open_slots.json`` lists the slot key.
_SEEDED_SLOT = "restart-hydration-slot"
_UNSEEDED_SLOT = "no-such-slot-ever"

# The WebSocket replay is a burst with no end-of-replay marker, so the reader
# waits for a specific run's ``subagent_done`` frame as the completion signal and
# fails on this deadline rather than treating an idle socket as "replay done".
_REPLAY_DEADLINE_SECS = 20.0


def _plant_persisted_run(*, agent_id: str, parent_session: str, age_secs: float = 120.0) -> None:
    """Write one finished persistent subagent run folder into the registry dir.

    The same on-disk shape ``test_subagent_panel_durable_replay.write_record``
    produces: a ``state.json`` identifying a persistent run owned by
    *parent_session*, plus a ``delivered`` ``tombstone.json`` so the reader
    classifies it as a completed run rather than waiting for a reconciler to
    stamp the ending. Written under ``subagent_persistence._subagents_dir()``,
    the registry the panel reader walks -- the rootdir conftest pins that to a
    per-test directory (``_isolate_subagents_dir``), which survives the restart
    because it is a module global the restart does not reset, so the replacement
    boot reads exactly this tree.

    *age_secs* places the run that far in the past, and the folder's mtime is
    stamped to match. The folder reader orders candidates by MTIME, newest-first,
    and stops the reconnect reader at a chosen run's frame; a caller that needs a
    run to be enumerated BEFORE a completion beacon gives the beacon a LARGER
    *age_secs* so its folder is older and folds last. The run times recorded in
    ``state.json``/``tombstone.json`` are kept consistent with the mtime so the
    record and the sort key tell the same story.
    """
    moment = time.time()
    started = moment - age_secs
    died = moment - age_secs / 2
    folder = _subagents_dir() / agent_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(
        json.dumps(
            {
                "id": agent_id,
                "task": _PLANTED_TASK,
                "agent": _PLANTED_AGENT,
                "parent_session": parent_session,
                "started": started,
                "status": "running",
                "turns": 2,
                "memory_mode": "persistent",
                "execution_context": {"memory_mode": "persistent"},
                "app": "",
                "updated_at": died,
            }
        ),
        encoding="utf-8",
    )
    (folder / "tombstone.json").write_text(
        json.dumps(
            {
                "id": agent_id,
                "cause": "delivered",
                "recovery_action": "delivered",
                "started": started,
                "died": died,
            }
        ),
        encoding="utf-8",
    )
    # The folder reader sorts candidates by st_mtime (newest first). Stamp it so
    # the fold order is the age order above and not an artefact of write order --
    # the beacon (older age) must fold after the audited run, so the reader can
    # only reach the beacon's frame by first enumerating the audited run.
    os.utime(folder, (died, died))


def _plant_open_slots(home: Path, slot_keys: list[str]) -> None:
    """Name *slot_keys* in ``open_slots.json`` so the next boot rehydrates them.

    ``restore_open_slots`` reads ``<home>/open_slots.json``'s ``keys`` list on
    every startup and rehydrates each from its session transcript -- the real
    slot-seeding path. Writing the file directly makes the restore deterministic
    rather than depending on a periodic-flush snapshot landing before restart.
    """
    (home / "open_slots.json").write_text(
        json.dumps({"keys": slot_keys, "ts": time.time()}), encoding="utf-8"
    )


def _plant_slot_transcript(home: Path, slot: str) -> None:
    """Write a minimal dashboard session transcript so boot can rehydrate *slot*.

    ``restore_open_slots`` reads each ``open_slots.json`` key's transcript from
    ``<home>/sessions/<safe_key>.jsonl`` and rebuilds the slot from its metadata
    -- the same shape the ``rich`` fixture ships for its seeded sessions. Written
    straight to disk (one metadata line, one user message) rather than through
    the store API: a store mutation on the event loop is refused under the
    integration layer's ``KIROCREW_STRICT_ON_LOOP_PERSIST`` guard, and the slot
    only has to exist on disk for the restart's restore to find it.
    """
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"{_safe_key(f'dashboard:{slot}')}.jsonl"
    lines = [
        {
            "_type": "metadata",
            "created_at": "2026-01-15T09:00:00+00:00",
            "last_consolidated": 0,
            "closed": False,
            "title": "restart hydration slot",
            "agent": "default",
        },
        {
            "role": "user",
            "content": "hello from the pre-restart slot",
            "ts": "2026-01-15T09:00:05+00:00",
            "source_thread": "dashboard",
            "source_user": "dashboard",
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


async def _subscribe_subagents_until(gw, *, until_done_id: str) -> dict[str, dict]:
    """Open the owner WebSocket, subscribe the panel, read until *until_done_id* lands.

    The owner session cookie the boot minted (``gw._cookie``) is replayed on the
    ``/api/ws`` upgrade so the socket authenticates as the dashboard owner; the
    first ``slots`` frame proves it registered. ``subscribe_subagents`` triggers
    the reconnect replay, whose frames (individually, or collapsed into one
    ``subagent_snapshot_batch`` above the batch threshold) are drained flat.

    The handler sends no end-of-replay marker, so the read is bounded by waiting
    for the ``subagent_done`` frame of *until_done_id* -- a run the caller knows
    is seeded and so is guaranteed to replay. Reaching it means the replay has
    produced every earlier frame too, which is the positive completion signal the
    caller then inspects. A missing frame expires ``_REPLAY_DEADLINE_SECS`` and
    fails the test on timeout rather than passing on an idle socket.

    Returns the collected ``subagent_done`` frames keyed by run id.
    """
    done: dict[str, dict] = {}
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"{gw.base_url}/api/ws", headers={"Cookie": gw._cookie}
        ) as ws:
            first = await ws.receive_json(timeout=15)
            assert first.get("type") == "slots", f"owner WS did not register: {first!r}"
            await ws.send_json({"type": "subscribe_subagents"})
            deadline = time.monotonic() + _REPLAY_DEADLINE_SECS
            while until_done_id not in done:
                remaining = deadline - time.monotonic()
                assert remaining > 0, (
                    f"replay never produced subagent_done for {until_done_id!r} "
                    f"within {_REPLAY_DEADLINE_SECS}s; collected={sorted(done)}"
                )
                message = await ws.receive(timeout=remaining)
                if message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
                if message.type != aiohttp.WSMsgType.TEXT:
                    continue
                event = json.loads(message.data)
                items = (
                    event.get("data", {}).get("items", [])
                    if event.get("type") == "subagent_snapshot_batch"
                    else [event]
                )
                for item in items:
                    if item.get("type") == "subagent_done":
                        done[str(item.get("data", {}).get("id"))] = item
    assert until_done_id in done, (
        f"owner WS closed before the beacon {until_done_id!r} replayed; "
        f"collected={sorted(done)}"
    )
    return done


@pytest.mark.asyncio
async def test_a_persisted_run_replays_after_its_slot_is_seeded_on_a_restart(
    integration_home, monkeypatch
):
    """``restore_open_slots`` rebuilds the slot, and the run owned by it replays.

    The slot's transcript, its ``open_slots.json`` entry and the persisted run
    folder are planted on the home before the first boot, so the production
    ``restore_open_slots`` seeds the slot on every boot and shutdown persists it
    (the slot is genuinely live, never pruned). The restart is then a true second
    boot whose ``restore_open_slots`` makes the slot live again; the reconnect
    replay reads it and the persisted run replays. The reader waits for that
    run's ``subagent_done`` frame under a deadline, so a dropped or slow frame
    fails the test rather than passing on an idle socket.

    ``KIROCREW_CREW_LOG`` is turned off so the replay's durable source is the run
    FOLDER reader (``read_panel_records``), which enumerates every run folder in
    the registry and then applies the per-record slot gates -- the exact gates
    the negative control relies on firing. With the crew log on, the durable fold
    is driven by the live slot NAMES, so an unseeded run is never a candidate and
    those gates are never reached; pinning the folder reader is what lets both
    tests exercise the one admission path that depends on the slot being seeded.
    """
    monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
    _plant_slot_transcript(integration_home, _SEEDED_SLOT)
    _plant_open_slots(integration_home, [_SEEDED_SLOT])
    _plant_persisted_run(
        agent_id=_PLANTED_RUN_ID,
        parent_session=f"dashboard:{_SEEDED_SLOT}",
    )

    async with booted_gateway(integration_home) as gw:
        # First boot already seeded the slot from the planted files; the restart
        # is the measured cold-start -- its restore seeds the slot again before
        # the replay below.
        assert gw.state.get_slot(_SEEDED_SLOT) is not None, "first boot did not seed the slot"
        await gw.restart()
        assert gw.state.get_slot(_SEEDED_SLOT) is not None, (
            "restore_open_slots did not rehydrate the slot on the replacement boot, "
            "so the replay below could never find it"
        )
        done = await _subscribe_subagents_until(gw, until_done_id=_PLANTED_RUN_ID)

    data = done[_PLANTED_RUN_ID]["data"]
    assert data["slot"] == _SEEDED_SLOT
    assert data["outcome"] == "completed"
    assert data["task"] == _PLANTED_TASK
    assert data["agent"] == _PLANTED_AGENT


@pytest.mark.asyncio
async def test_a_persisted_run_whose_slot_is_absent_is_withheld_on_a_restart(
    integration_home, monkeypatch
):
    """Negative control: without the seeded slot, the same record is dropped.

    Proves the positive test is the slot seeding carrying the record through, not
    the record merely existing on disk. The audited run is owned by a session
    with no ``open_slots.json`` entry, so no slot is restored for it; the slot is
    absent from ``_slots``, ``get_slot`` answers None, and a fail-closed slot gate
    withholds the frame. On this path -- the folder reader, read by a dashboard
    owner -- the gate that fires is the visibility PRE-CAP
    (``persisted_precap_denial_reason`` -> ``persisted_not_visible``), because the
    pre-cap's visible set is exactly the live slot keys and an unrestored slot is
    not among them; the authoritative ``slot_missing`` check
    (``persisted_replay_denial_reason``) is the belt-and-braces behind it, reached
    only by a record the pre-cap admitted. The mutation check in the PR body
    confirms the dependency: opening both the pre-cap and ``slot_missing`` makes
    this control go red, and either one left in place keeps it green.

    Two properties make the control able to FAIL if that gating regresses:

    * ``KIROCREW_CREW_LOG`` is off, so the durable source is the folder reader,
      which enumerates EVERY run folder regardless of which slots are live. The
      audited run is therefore a real candidate that reaches the per-record slot
      gates -- with the crew log on it would never be enumerated (the fold is
      keyed off live slot names), so the gates would not run and the control
      would pass vacuously.
    * The beacon is owned by the real seeded slot AND is planted OLDER than the
      audited run (a larger ``age_secs`` -> an older folder mtime), so the folder
      reader -- which sorts newest-first -- enumerates the audited run before the
      beacon. The reader stops at the beacon's ``subagent_done`` frame only after
      the audited run has been offered to the gate, so a gate that leaked the
      audited run would be observed here rather than skipped past.
    """
    monkeypatch.setenv("KIROCREW_CREW_LOG", "0")
    _plant_slot_transcript(integration_home, _SEEDED_SLOT)
    _plant_open_slots(integration_home, [_SEEDED_SLOT])
    # Audited run: newer (folds first), owned by the UNSEEDED slot -- the gate
    # must withhold it.
    _plant_persisted_run(
        agent_id=_PLANTED_RUN_ID,
        parent_session=f"dashboard:{_UNSEEDED_SLOT}",
        age_secs=120.0,
    )
    # The beacon is owned by the seeded slot, so it is guaranteed to replay; its
    # frame marks the replay observably complete. It is planted OLDER than the
    # audited run so the newest-first folder reader reaches the audited run
    # first -- the beacon frame can only arrive after the gate has judged it.
    _plant_persisted_run(
        agent_id=_BEACON_RUN_ID,
        parent_session=f"dashboard:{_SEEDED_SLOT}",
        age_secs=600.0,
    )

    async with booted_gateway(integration_home) as gw:
        await gw.restart()
        assert gw.state.get_slot(_SEEDED_SLOT) is not None, "the control slot was not seeded"
        assert gw.state.get_slot(_UNSEEDED_SLOT) is None, "the run's own slot should be absent"
        done = await _subscribe_subagents_until(gw, until_done_id=_BEACON_RUN_ID)

    assert _PLANTED_RUN_ID not in done, (
        "a persisted run whose owning slot was never seeded replayed anyway; the "
        f"restart's fail-closed slot gate should have withheld it. done={sorted(done)}"
    )
