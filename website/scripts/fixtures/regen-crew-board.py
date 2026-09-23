#!/usr/bin/env python3
"""Regenerate the crew-board screenshot fixtures from the REAL handler.

The fixtures are the payload the capture harness renders, and the harness claims
they are the handler's verbatim output. That claim only stays true if they are
regenerated whenever the response shape changes -- otherwise they carry fields the
handler no longer emits and a reader trusting them sees a shape that cannot occur.

Run from the repo root with the worktree's venv:
    KIROCREW_HOME=<scratch> ./.venv/bin/python <this file>
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

if "KIROCREW_HOME" not in os.environ:
    sys.exit("refusing to run without KIROCREW_HOME pointed at a scratch dir")

sys.path.insert(0, "src")

from types import SimpleNamespace  # noqa: E402

from aiohttp.test_utils import make_mocked_request  # noqa: E402

from kiro_crew import work_ledger as wl  # noqa: E402
from kiro_crew.dashboard.handlers import work_ledger_board as board  # noqa: E402

OUT = pathlib.Path("website/scripts/fixtures")
OWNER = "owner-subject"

ALIVE = "chat-1750-conductor"
ORPHAN = "chat-1888-orphan-conductor"
EMPTY = "chat-1901-empty-conductor"


def _slot(*, running: bool = False):
    return SimpleNamespace(running=running)


def _state(slots: dict):
    return SimpleNamespace(_slots=dict(slots), get_slot=lambda k: slots.get(k), owner_id=OWNER)


def _create(conductor: str, title: str, acceptance: dict) -> str:
    return wl.apply_conductor_action(conductor, "create", title=title, acceptance=acceptance)[
        "item"
    ].item_id


async def _read(state, conductor: str) -> dict:
    request = make_mocked_request(
        "GET", f"/api/crew-board?conductor={conductor}", app={"state": state}
    )
    request["user"] = OWNER
    request["app"] = ""
    response = await board.api_work_ledger_board(request)
    assert response.status == 200, response.body
    return json.loads(response.body.decode())


def seed_alive() -> dict:
    """The ordinary board: a conductor still reading its own reports."""
    wl.ensure_conductor(ALIVE, goal="Ship the conductor work ledger end to end (RFC Phases 1-5)")

    wake = _create(ALIVE, "Wake hook on the ledger append path", {"kind": "human_approval"})
    wl.apply_conductor_action(ALIVE, "bind", item_id=wake, worker_session_key="chat-1751-w1")
    wl.apply_worker_report(
        ALIVE,
        wake,
        status="question",
        summary=(
            "Append fires inside the store lock. Waking there risks re-entry; "
            "waking after the release can miss a write. Which boundary owns it?"
        ),
        artifacts={"branch": "feat/ledger-wake-hook"},
    )

    mask = _create(ALIVE, "Mask the worker key out of every browser payload", {"kind": "pr_checks"})
    wl.apply_conductor_action(ALIVE, "bind", item_id=mask, worker_session_key="chat-1752-w2")
    wl.apply_worker_report(
        ALIVE,
        mask,
        status="done",
        summary=(
            "Item field and the bind event's text both masked; whole-payload "
            "assertion added so a new field cannot leak by default."
        ),
        pr=12195,
    )
    wl.apply_conductor_action(ALIVE, "verdict", item_id=mask, verdict="pass", fails=0)
    wl.apply_conductor_action(
        ALIVE,
        "close",
        item_id=mask,
        state="accepted",
        decision="Verified against the bar. Accepted.",
    )

    _create(ALIVE, "Stall sweep for items whose worker went quiet", {"kind": "human_approval"})

    blocked = _create(ALIVE, "Backfill the event log schema version", {"kind": "human_approval"})
    wl.apply_conductor_action(ALIVE, "bind", item_id=blocked, worker_session_key="chat-1754-w4")
    wl.apply_worker_report(
        ALIVE,
        blocked,
        status="blocked",
        summary="Needs the migration in #12180 to land first; nothing here can move until then.",
    )

    page = _create(ALIVE, "Crew page item table over the masked projection", {"kind": "pr_checks"})
    wl.apply_conductor_action(ALIVE, "bind", item_id=page, worker_session_key="chat-1755-w5")
    wl.apply_conductor_action(
        ALIVE,
        "decide",
        item_id=page,
        decision="Fold onto the incumbent RFC's Phase 4. Build the masked projection first.",
    )
    wl.apply_worker_report(
        ALIVE,
        page,
        status="progress",
        summary="Masked projection landed with 31 tests. Page, bands and the 10 s poll are in.",
        pr=12208,
        artifacts={"branch": "feat/crew-page-work-items", "commit": "a523538a5f"},
    )

    # The conductor's own slot is OPEN, so nothing is orphaned. One worker running,
    # one closed, so `alive` is joined from something real rather than constant.
    return {
        ALIVE: _slot(),
        "chat-1751-w1": _slot(),
        "chat-1755-w5": _slot(running=True),
    }


def seed_orphan() -> dict:
    """The state the affordances are named for: the conductor's slot is gone."""
    wl.ensure_conductor(ORPHAN, goal="Land the ledger wake hook before the freeze")

    rebase = _create(
        ORPHAN, "Rebase the wake hook onto the rewritten append path", {"kind": "human_approval"}
    )
    wl.apply_conductor_action(ORPHAN, "bind", item_id=rebase, worker_session_key="chat-1889-w1")
    wl.apply_worker_report(
        ORPHAN,
        rebase,
        status="question",
        summary="Two call sites now append under the lock. Which one owns the wake?",
        artifacts={"branch": "feat/wake-hook-rebase"},
    )

    retire = _create(ORPHAN, "Retire the duplicated append helper", {"kind": "human_approval"})
    wl.apply_conductor_action(ORPHAN, "bind", item_id=retire, worker_session_key="chat-1889-w2")
    wl.apply_worker_report(
        ORPHAN,
        retire,
        status="progress",
        summary="Helper inlined at both call sites; the old one is unreferenced now.",
        artifacts={"branch": "feat/retire-append-helper"},
    )

    # No slot for ORPHAN itself: that absence is what makes both items orphaned.
    # One worker still running and one closed, which is the differential the shot
    # exists to show -- Stop is live on the first and refused on the second.
    return {"chat-1889-w1": _slot(running=True)}


def seed_empty() -> dict:
    """A ledger that exists and holds nothing.

    Distinct from the no-ledger case: the route answers 200 here, so the page shows
    its empty state rather than its no-ledger one, and a reader cannot tell the two
    apart from a board shot without seeing both.
    """
    wl.ensure_conductor(EMPTY, goal="Split the launcher rewrite into shippable items")
    return {EMPTY: _slot()}


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    for name, conductor, seed in (
        ("alive", ALIVE, seed_alive),
        ("orphaned", ORPHAN, seed_orphan),
        ("empty", EMPTY, seed_empty),
    ):
        slots = seed()
        payload = await _read(_state(slots), conductor)

        blob = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        # The masking criterion, asserted on the bytes actually written rather than
        # on the object: a fixture that leaks is a fixture committed to the repo.
        assert "worker_session_key" not in blob, f"{name}: masked field present"
        assert "chat-1751" not in blob and "chat-1889" not in blob, f"{name}: worker key present"

        (OUT / f"crew-board-{name}.json").write_text(blob, encoding="utf-8")
        print(f"{name:9} keys={sorted(payload)} items={len(payload['items'])}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
