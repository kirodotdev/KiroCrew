"""``dashboard_save`` answers are held byte for byte across the writer it shares.

``dashboard_add_field`` writes a page through the helper the save ends in
(``agent_panel_dashboard._store_bound_package``). The file beside this one,
``fixtures/dashboard_save_answers.json``, holds the status and the body TEXT the save
route gives on the cases below. The unit runs the same cases and compares byte for
byte, so a change to the helper made for another route cannot move the save's reply
unseen. A change to the save's reply made on purpose rewrites the file with it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from test_dashboard_save_tool import (  # noqa: F401  (``store`` is a fixture)
    SLUG,
    _client,
    _flip,
    _instance,
    _package_body,
    _save,
    store,
)

from kiro_crew import widgets_preview
from kiro_crew.dashboard.handlers import agent_panel as routes

#: The fixture this module takes by name from the module that defines it.
_FIXTURES = (store,)

ANSWERS = Path(__file__).parent / "fixtures" / "dashboard_save_answers.json"


async def _checked(*_a: Any, **_k: Any) -> dict[str, Any]:
    """The page check, fixed: a host with no browser, which is what a CI runner is."""
    return {"status": "skipped", "reason": "no_browser"}


def _with(change: Any) -> dict[str, Any]:
    body = _package_body()
    change(body)
    return body


def _many_fields(body: dict[str, Any]) -> None:
    for n in range(64):
        body["model"]["types"][f"extra_{n}"] = {"type": "number", "source": {"agentic": True}}


def _widgets(body: dict[str, Any]) -> None:
    body["widgets"] = [
        {
            "id": "prs",
            "title": "PRs",
            "sizes": ["small"],
            "blocks": [{"type": "metric", "label": "Open PRs", "field": "open_prs"}],
        }
    ]


async def save_answers(monkeypatch: Any, store: Any) -> dict[str, list[Any]]:
    """Each case's ``[status, body text]``, in a fixed order over one store."""
    monkeypatch.setattr(routes, "_page_check", _checked)
    monkeypatch.setattr(widgets_preview, "widgets_enabled", lambda: False)
    out: dict[str, list[Any]] = {}

    async def _take(name: str, client: Any, body: Any) -> None:
        resp = await _save(client, body)
        out[name] = [resp.status, await resp.text()]

    token = "npm_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8"
    async with _client(monkeypatch) as client:
        await _take("a body key the schema does not hold", client, {**_package_body(), "x": 1})
        await _take(
            "a bound_to in the body", client, {**_package_body(), "bound_to": "crewmate:other"}
        )
        await _take("no model", client, {"view": {"html": "<p></p>"}})
        await _take("widgets while the preview is off", client, _with(_widgets))
        await _take(
            "a name a redactor would mask",
            client,
            _with(
                lambda b: b["model"]["types"].update(
                    {token: {"type": "text", "source": {"agentic": True}}}
                )
            ),
        )
        await _take(
            "a type outside the catalog",
            client,
            _with(lambda b: b["model"]["types"]["open_prs"].update({"type": "money"})),
        )
        await _take("a 65th field", client, _with(_many_fields))
        await _take(
            "a cell bound to a field not declared",
            client,
            _with(lambda b: b["view"].update({"html": '<p data-dashboard-field="nope"></p>'})),
        )
        await _take("a first save", client, _package_body())
        await _take("a second save of another layout", client, _with(_flip))
        await _take("a save of the same layout again", client, _with(_flip))
        await _take(
            "a save with a hand-written and a fold field",
            client,
            _with(
                lambda b: b["model"]["types"].update(
                    {"work_rows": {"type": "array", "source": {"fold": "work", "path": "items"}}}
                )
            ),
        )

        def _down(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("the disk is full")

        monkeypatch.setattr(store, "update", _down)
        await _take("a store that cannot write", client, _package_body())
    session = _instance().session_instance_key("chat-7-17910")
    async with _client(monkeypatch, resolved=(session, "", "chat-7-17910")) as client:
        await _take("a root session", client, _package_body())
    async with _client(monkeypatch, resolved=None) as client:
        await _take("a caller the gate turns away", client, _package_body())
    return out


@pytest.mark.asyncio
async def test_the_save_answers_byte_for_byte_what_the_file_holds(monkeypatch, store):
    """Status and body text of every case equal the recorded ones."""
    recorded = json.loads(ANSWERS.read_text(encoding="utf-8"))
    now = await save_answers(monkeypatch, store)
    assert list(now) == list(recorded)
    for name, answer in now.items():
        assert answer == recorded[name], name
    # The cases are not all one answer: a refusal of each kind and four saves.
    assert sorted({a[0] for a in now.values()}) == [200, 400, 403, 503]
    assert len(store.records) == 1, SLUG
