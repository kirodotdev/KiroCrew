"""The ``workflow.save`` and ``workflow.update`` change cards: an agent asks, the owner saves.

The catalog half (params, preview, risk, apply plan, no undo) is pure. The end-to-end
half runs the REAL card routes, the REAL card hook and the REAL workflow-definition
handlers over a real ``WorkflowDefinitionLibrary`` in a temporary directory: the agent
proposes on the internal credential, its own direct write is refused, nothing is
saved until the owner's browser sends the card's step, a decline saves nothing, and
an update appends a revision while the earlier one stays.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from guide_route_helpers import in_dashboard_turn

import kiro_crew.dashboard.handlers.workflows as workflow_handlers
from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.dashboard.handlers.workflows import (
    api_workflow_definition_update,
    api_workflow_definitions_create,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.workflows.library import WorkflowDefinitionLibrary
from kiro_crew.workflows.validate import validate

SAVE = "workflow.save"
UPDATE = "workflow.update"
SLOT = "chat-1"
SK = f"dashboard:{SLOT}"

SOURCE_V1 = (
    'META = {"name": "investigate", "description": "Find the cause of a failure"}\n'
    "async def workflow(ctx):\n"
    '    return await ctx.agent("investigate")\n'
)
SOURCE_V2 = SOURCE_V1.replace('ctx.agent("investigate")', 'ctx.agent("investigate and report")')


def _save_params(**extra: Any) -> dict[str, Any]:
    return {"source": SOURCE_V1, "name": "Investigate", **extra}


# ── catalog ──


@pytest.mark.parametrize(
    "kind,params,code",
    [
        (SAVE, {"name": "x"}, "missing_param"),
        (SAVE, {"source": SOURCE_V1}, "missing_param"),
        (SAVE, {"source": "   ", "name": "x"}, "invalid_param"),
        (SAVE, {"source": "x" * (catalog.WORKFLOW_SOURCE_MAX + 1), "name": "x"}, "invalid_param"),
        (SAVE, {**_save_params(), "slug": "Not A Slug"}, "invalid_param"),
        (SAVE, {**_save_params(), "format": "task-plan"}, "unknown_param"),
        (SAVE, {**_save_params(), "derived_from": {}}, "unknown_param"),
        (UPDATE, {"source": SOURCE_V2}, "missing_param"),
        (UPDATE, {"workflow": "../x", "source": SOURCE_V2}, "invalid_param"),
        (UPDATE, {"workflow": "wfd_1", "source": SOURCE_V2, "slug": "x"}, "unknown_param"),
    ],
)
def test_bad_params_are_refused(kind, params, code):
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(kind, params)
    assert exc.value.code == code


def test_both_kinds_are_listed_for_list_change_kinds():
    listed = {k["id"] for k in catalog.list_kinds()}
    assert {SAVE, UPDATE} <= listed


def test_windows_line_ends_are_saved_as_the_card_shows_them():
    p = catalog.validate_params(SAVE, _save_params(source=SOURCE_V1.replace("\n", "\r\n")))
    assert p["source"] == SOURCE_V1.strip()


def test_save_preview_shows_the_whole_script_and_plans_the_library_post():
    p = catalog.validate_params(
        SAVE, _save_params(description="Find the cause", slug="investigate")
    )
    preview = catalog.build_preview(SAVE, p, {}, {})
    assert preview["title"] == "Save workflow “Investigate” to the library"
    assert preview["risk"] == "code_exec"
    assert preview["changes"] == [
        {"field": "name", "label": "Name", "after": "Investigate"},
        {"field": "description", "label": "Description", "after": "Find the cause"},
        {"field": "workflow_command", "label": "Command", "after": "/workflow investigate"},
        {"field": "workflow_source", "label": "Script", "after": SOURCE_V1.strip()},
    ]
    assert preview["apply"] == [
        {
            "method": "POST",
            "path": "/api/workflows/definitions",
            "body": {
                "source": SOURCE_V1.strip(),
                "name": "Investigate",
                "description": "Find the cause",
                "slug": "investigate",
            },
        }
    ]
    assert preview["undo_unavailable_reason"] == catalog.UNDO_NO_IDENTITY_CHECK


def _update_snapshot(**extra: Any) -> dict[str, Any]:
    return {
        "exists": True,
        "id": "wfd_0123456789abcdef",
        "slug": "investigate",
        "name": "Investigate",
        "description": "",
        "format": "python",
        "revision": 2,
        "content_hash": "h",
        **extra,
    }


def test_update_preview_shows_the_diff_and_pins_the_revision():
    p = catalog.validate_params(UPDATE, {"workflow": "investigate", "source": SOURCE_V2})
    diff = cards._workflow_diff(SOURCE_V1, SOURCE_V2, 2)
    preview = catalog.build_preview(UPDATE, p, _update_snapshot(), {"diff": diff})
    assert preview["title"] == "Save a new revision of workflow “Investigate”"
    assert preview["risk"] == "code_exec"
    rows = {row["label"]: row for row in preview["changes"]}
    assert rows["Workflow"]["after"] == "/workflow investigate"
    assert (rows["Revision"]["before"], rows["Revision"]["after"]) == (2, 3)
    assert rows["Changes to the script"]["after"] == diff
    assert '-    return await ctx.agent("investigate")' in diff
    assert rows["Script"]["detail"] is True
    assert preview["apply"] == [
        {
            "method": "PATCH",
            "path": "/api/workflows/definitions/wfd_0123456789abcdef",
            "body": {"source": SOURCE_V2.strip(), "expected_revision": 2},
        }
    ]
    assert preview["undo_unavailable_reason"] == catalog.UNDO_NO_IDENTITY_CHECK


def test_update_refuses_a_missing_or_non_python_definition_and_no_change():
    p = catalog.validate_params(UPDATE, {"workflow": "investigate", "source": SOURCE_V2})
    for snapshot, code in (
        ({"exists": False}, "not_found"),
        (_update_snapshot(format="task-plan"), "not_python"),
        (_update_snapshot(), "no_change"),
    ):
        with pytest.raises(catalog.CardCatalogError) as exc:
            catalog.build_preview(UPDATE, p, snapshot, {})
        assert exc.value.code == code


def test_update_that_only_renames_sends_the_new_name():
    p = catalog.validate_params(
        UPDATE, {"workflow": "investigate", "source": SOURCE_V1, "name": "Investigate v2"}
    )
    preview = catalog.build_preview(UPDATE, p, _update_snapshot(), {})
    assert {
        "field": "name",
        "label": "Name",
        "before": "Investigate",
        "after": "Investigate v2",
    } in (preview["changes"])
    assert preview["apply"][0]["body"]["name"] == "Investigate v2"


def test_a_long_diff_is_cut_and_points_at_the_whole_script():
    old = "\n".join(f"old line {i}" for i in range(3000))
    new = "\n".join(f"new line {i}" for i in range(3000))
    diff = cards._workflow_diff(old, new, 1)
    assert len(diff) <= cards.WORKFLOW_DIFF_MAX + 200
    assert diff.splitlines()[-1].endswith("the whole new script is under Details")


def test_neither_card_offers_undo():
    for kind, params in (
        (SAVE, _save_params()),
        (UPDATE, {"workflow": "investigate", "source": SOURCE_V2}),
    ):
        p = catalog.validate_params(kind, params)
        plan, reason = catalog.build_undo(kind, p, {}, [{}], 1)
        assert plan is None and reason == catalog.UNDO_NO_IDENTITY_CHECK


def test_the_library_routes_are_hooked_but_write_no_memory_line():
    for route in (
        ("POST", "/api/workflows/definitions"),
        ("PATCH", "/api/workflows/definitions/{workflow_ref}"),
    ):
        assert route in catalog.HOOKED_ROUTES
        assert route not in catalog.MEMORY_ROUTES


# ── end to end: real card routes, real hook, real definition routes ──


class _Service:
    """The workflow service's definition calls, over a real library on disk."""

    def __init__(self, library: WorkflowDefinitionLibrary) -> None:
        self.library = library

    def get_definition(self, workflow_ref: str):
        return self.library.get(workflow_ref)

    def save_definition(self, source: str, *, name="", description="", slug="", **_kw):
        result = validate(source)
        if not result.ok:
            return {"ok": False, "error": "; ".join(result.errors), "errors": result.errors}
        return {
            "ok": True,
            "definition": self.library.create(
                source=source, name=name, description=description, slug=slug
            ),
        }

    def update_definition(self, workflow_ref: str, *, source: str, expected_revision: int, **kw):
        current = self.library.get(workflow_ref)
        if current is None:
            return {"ok": False, "error": "no such saved workflow", "not_found": True}
        updated = self.library.update(
            current["id"], source=source, expected_revision=expected_revision, **kw
        )
        if updated is None:
            return {"ok": False, "error": "newer revision", "conflict": True}
        return {"ok": True, "definition": updated}


class _State:
    """The slice of ``DashboardState`` the card routes and the definition routes read."""

    owner_id = ""

    def __init__(self, service: _Service | None) -> None:
        self._slots: dict[str, _ChatSlot] = {SLOT: in_dashboard_turn(_ChatSlot(SLOT))}
        self._change_card_store = CardStore(None)
        self.workflow_service = service
        self.conversation_log = None
        self.push_slots_update = MagicMock()

    def get_slot(self, name: str):
        return self._slots.get(name)

    async def deliver_ws_owners(self, kind: str, payload: dict[str, Any]) -> int:
        return 1

    def broadcast_ws(self, kind: str, payload: dict[str, Any]) -> None:
        return None


@web.middleware
async def _fake_auth(request: web.Request, handler):
    if request.headers.get("X-Test-Auth") == "internal":
        # The internal credential carries no ``app`` claim, as on the real gateway,
        # so the definition routes' dashboard-user gate refuses it.
        request["user"] = "kiro-cli"
        request["internal_auth"] = True
        request["peer_verified"] = True
    else:
        request["app"] = ""
        request["user"] = "local-app"
    return await handler(request)


class _Audit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)

    def operations(self) -> list[tuple[str, str]]:
        return [
            (str(r["operation"]), str(r["outcome"]))
            for r in self.rows
            if str(r["operation"]).startswith("cards.workflow.")
        ]


def _run(state: _State, fn, monkeypatch):
    audit = _Audit()
    monkeypatch.setattr(routes, "sel", lambda: audit)
    monkeypatch.setattr(workflow_handlers, "_sel", lambda: audit)

    async def main():
        app = web.Application(middlewares=[_fake_auth, routes.change_card_middleware])
        app["state"] = state
        routes.register_change_card_routes(app)
        app.router.add_post("/api/workflows/definitions", api_workflow_definitions_create)
        app.router.add_patch(
            "/api/workflows/definitions/{workflow_ref}", api_workflow_definition_update
        )
        async with TestClient(TestServer(app)) as client:
            await fn(client)
            if routes._BACKGROUND:
                await asyncio.gather(*list(routes._BACKGROUND))

    asyncio.run(main())
    return audit


AGENT = {"X-Test-Auth": "internal", "X-Session-Key": SK}


def _step(card: dict[str, Any]) -> dict[str, str]:
    return {
        "X-Test-Auth": "owner",
        "X-Card-Id": card["id"],
        "X-Card-Revision": str(card["revision"]),
        "X-Card-Op": "apply",
        "X-Card-Step": "0",
    }


async def _propose(c: TestClient, kind: str, params: dict[str, Any], headers=AGENT):
    return await c.post(
        "/api/cards/agent/propose", json={"kind": kind, "params": params}, headers=headers
    )


def test_agent_proposes_and_only_the_owners_press_saves(tmp_path, monkeypatch):
    library = WorkflowDefinitionLibrary(tmp_path)
    state = _State(_Service(library))

    async def go(c):
        # The agent cannot write the library itself...
        r = await c.post(
            "/api/workflows/definitions", json={"source": SOURCE_V1, "name": "x"}, headers=AGENT
        )
        assert r.status == 403, await r.text()
        assert library.list() == []
        # ...it proposes the card, which saves nothing yet.
        r = await _propose(c, SAVE, _save_params(slug="investigate"))
        assert r.status == 200, await r.text()
        card = await r.json()
        assert card["kind"] == SAVE and card["risk"] == "code_exec"
        assert library.list() == []
        step = card["plan"]["apply"][0]
        # The agent's own credential cannot press the card's button.
        r = await c.post(step["path"], json=step["body"], headers={**_step(card), **AGENT})
        assert r.status == 403
        assert library.list() == []
        # The owner's browser saves it through the Workflows tab's own route.
        r = await c.post(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 201, await r.text()
        saved = library.list()
        assert [(d["name"], d["slug"], d["source"]) for d in saved] == [
            ("Investigate", "investigate", SOURCE_V1.strip())
        ]
        r = await c.get(f"/api/cards/agent/status?card_id={card['id']}", headers=AGENT)
        applied = await r.json()
        assert applied["status"] == "applied", applied
        assert not applied["plan"].get("undo")

    audit = _run(state, go, monkeypatch)
    assert audit.operations() == [
        ("cards.workflow.propose", "ok"),
        ("cards.workflow.apply", "completed"),
    ]


def test_a_declined_card_saves_nothing(tmp_path, monkeypatch):
    library = WorkflowDefinitionLibrary(tmp_path)
    state = _State(_Service(library))

    async def go(c):
        r = await _propose(c, SAVE, _save_params())
        card = await r.json()
        r = await c.post(
            f"/api/cards/{card['id']}/cancel",
            json={"revision": card["revision"]},
            headers={"X-Test-Auth": "owner"},
        )
        assert r.status == 200, await r.text()
        assert (await r.json())["status"] == "cancelled"
        step = card["plan"]["apply"][0]
        r = await c.post(step["path"], json=step["body"], headers=_step(card))
        assert r.status >= 400
        assert library.list() == []

    audit = _run(state, go, monkeypatch)
    assert audit.operations() == [
        ("cards.workflow.propose", "ok"),
        ("cards.workflow.decline", "rejected"),
    ]


def test_an_update_appends_a_revision_and_keeps_the_old_one(tmp_path, monkeypatch):
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Investigate", slug="investigate")
    state = _State(_Service(library))

    async def go(c):
        r = await _propose(c, UPDATE, {"workflow": "investigate", "source": SOURCE_V2})
        assert r.status == 200, await r.text()
        card = await r.json()
        labels = [row["label"] for row in card["changes"]]
        assert "Changes to the script" in labels
        assert library.get(created["id"])["revision"] == 1
        step = card["plan"]["apply"][0]
        assert step["path"] == f"/api/workflows/definitions/{created['id']}"
        r = await c.patch(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 200, await r.text()
        current = library.get(created["id"])
        assert current["revision"] == 2
        assert current["source"] == SOURCE_V2.strip()
        assert [rev["source"] for rev in current["revisions"]] == [SOURCE_V1, SOURCE_V2.strip()]

    _run(state, go, monkeypatch)


def test_an_update_refuses_a_revision_saved_after_the_preview(tmp_path, monkeypatch):
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Investigate", slug="investigate")
    state = _State(_Service(library))

    async def go(c):
        r = await _propose(c, UPDATE, {"workflow": "investigate", "source": SOURCE_V2})
        card = await r.json()
        other = SOURCE_V1.replace('ctx.agent("investigate")', 'ctx.agent("look into it")')
        library.update(created["id"], source=other, expected_revision=1)
        step = card["plan"]["apply"][0]
        r = await c.patch(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 409, await r.text()
        assert library.get(created["id"])["source"] == other

    _run(state, go, monkeypatch)


def test_an_invalid_script_never_becomes_a_card(tmp_path, monkeypatch):
    state = _State(_Service(WorkflowDefinitionLibrary(tmp_path)))

    async def go(c):
        r = await _propose(
            c, SAVE, _save_params(source="import os\nasync def workflow(ctx):\n    pass\n")
        )
        assert r.status == 400, await r.text()
        assert (await r.json())["code"] == "invalid_workflow"
        assert state._change_card_store.pending(None) == []

    _run(state, go, monkeypatch)


def test_no_card_without_the_workflow_service(monkeypatch):
    state = _State(None)

    async def go(c):
        r = await _propose(c, SAVE, _save_params())
        assert r.status == 400, await r.text()
        assert (await r.json())["code"] == "workflows_unavailable"

    _run(state, go, monkeypatch)


def test_a_crewmate_cannot_propose_a_saved_workflow(tmp_path, monkeypatch):
    from kiro_crew.dashboard import mate_thread_wakes

    monkeypatch.setattr(mate_thread_wakes, "is_crewmate_thread", lambda slot: True)
    library = WorkflowDefinitionLibrary(tmp_path)
    state = _State(_Service(library))

    async def go(c):
        for kind, params in (
            (SAVE, _save_params()),
            (UPDATE, {"workflow": "investigate", "source": SOURCE_V2}),
        ):
            r = await _propose(c, kind, params)
            assert r.status == 403, await r.text()
            assert (await r.json())["code"] == "owner_assistant_only"
        assert state._change_card_store.pending(None) == []

    _run(state, go, monkeypatch)


def test_a_subagent_cannot_propose_a_saved_workflow(tmp_path, monkeypatch):
    state = _State(_Service(WorkflowDefinitionLibrary(tmp_path)))

    async def go(c):
        headers = {"X-Test-Auth": "internal", "X-Session-Key": "subagent:abc"}
        r = await _propose(c, SAVE, _save_params(), headers=headers)
        assert r.status == 403, await r.text()
        assert (await r.json())["code"] == "subagent_caller"

    _run(state, go, monkeypatch)
