"""The quick-ask wire contract, held against the fixture the card's tests read too.

``website/src/test/fixtures/quickAskContract.json`` is the one copy of the shape:
this file proves the gateway publishes every ``asks.*.wire`` exactly (no key
added, dropped or renamed), takes every ``answers`` body over the real owner
route, and records the replay ``person_event`` as written; the card's
``QuickAskContract.test.tsx`` proves the other half against the same file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_browser_quick_ask import _home  # noqa: F401 - the autouse hub/home reset
from test_browser_quick_ask import KEY, FakePage, _answer, _asked

from kiro_crew.browser_harness import live_hub as lh

pytestmark = pytest.mark.asyncio

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "website/src/test/fixtures/quickAskContract.json"
    ).read_text(encoding="utf-8")
)

#: The page element each role binds to, as the fake page describes it.
_NODE = {
    "username": {"type": "text"},
    "password": {"type": "password"},
    "text": {"type": "email"},
    "otp": {"type": "text"},
    "widget": {"tag": "iframe"},
    "action": {"tag": "button", "text": "Place order"},
}


async def _ask_for(kind: str) -> tuple[lh.LiveHub, Any, Any, FakePage]:
    spec = FIXTURE["asks"][kind]
    wire = spec["wire"]
    page = FakePage()
    page.frame["url"] = wire["origin"] + "/page"
    for target, field in zip(spec["args"]["targets"], wire["fields"]):
        role, _, ref = target.partition("=")
        assert role == field["role"]
        r = field["rect"]
        page.add(ref, **_NODE[role], rect=[r["x"], r["y"], r["w"], r["h"]])
    hub, rec, ask = await _asked(page, dict(spec["args"]))
    return hub, rec, ask, page


@pytest.mark.parametrize("kind", sorted(FIXTURE["asks"]))
async def test_the_gateway_publishes_the_fixture_ask_exactly(kind: str) -> None:
    wire = FIXTURE["asks"][kind]["wire"]
    hub, _rec, ask, _page = await _ask_for(kind)
    # The two values a real ask mints for itself, pinned to the fixture's.
    ask.id, ask.expires_at, ask.phase = wire["id"], wire["expires_at"], wire["phase"]
    assert lh.live_event(hub.get(KEY))["ask"] == wire  # type: ignore[arg-type]


@pytest.mark.parametrize("answer", FIXTURE["answers"], ids=lambda a: a["name"])
async def test_the_owner_route_takes_every_fixture_answer(answer: dict[str, Any]) -> None:
    _hub, _rec, ask, _page = await _ask_for(answer["kind"])
    if answer["body"]["action"] == "done":
        status, out, _ = await _answer({"ask_id": ask.id, "action": "take_over"})
        assert status == 200, out
    status, out, _ = await _answer({"ask_id": ask.id, **answer["body"]})
    assert status == 200 and out["ok"] is True, out


async def test_the_replay_records_the_fixture_person_event() -> None:
    expected = FIXTURE["person_event"]
    _hub, rec, ask, _page = await _ask_for(expected["kind"])
    values = next(a for a in FIXTURE["answers"] if a["name"] == "credentials_both")["body"]
    status, out, _ = await _answer({"ask_id": ask.id, **values})
    assert (status, out.get("outcome")) == (200, expected["outcome"]), out
    event = dict(rec.events[-1][1])
    assert isinstance(event.pop("ts"), int)
    want = dict(expected["event"])
    want.pop("ts")
    assert event == want
