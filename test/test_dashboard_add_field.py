"""``dashboard_add_field``: one more hand-written field on the caller's own page, as a draft.

Rows G1 to G9 of the plan, and the cases beside them. Each test names in its docstring
the change to the code that turns it red.

The route is ``POST /api/agent-panel/dashboard/add-field``. Most tests stub the caller
resolution, as the save's own tests do; G6 and the caller test run the real gate.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from agent_panel_routes_helpers import _client as _gated_client  # the real caller gate
from agent_panel_routes_helpers import vetted_fixture  # noqa: F401  (fixture ``vetted``)
from aiohttp.test_utils import TestClient
from test_dashboard_save_tool import (  # noqa: F401  (``store`` is a fixture)
    CREW,
    SLUG,
    _apply,
    _client,
    _flip,
    _instance,
    _package_body,
    _save,
    _save_and_apply,
    store,
)

from kiro_crew import mcp_panel, widgets_preview
from kiro_crew.artifact_store import dashboard_package as gate
from kiro_crew.artifact_store.dashboard_package import parse_package
from kiro_crew.artifact_store.model import ArtifactValidationError
from kiro_crew.dashboard.handlers import agent_panel as routes
from kiro_crew.dashboard.handlers import agent_panel_add_field as add_field
from kiro_crew.dashboard_templates import instance_applied
from kiro_crew.testing.wait import async_wait_until, default_timeout

#: Fixtures this module takes by name from the modules that define them.
_FIXTURES = (store, vetted_fixture)

PATH = "/api/agent-panel/dashboard/add-field"
#: Text only the caller wrote. No reply and no log line may hold it.
MARK = "zq7marker"
TOKEN_NAME = "npm_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8"
LAYOUT = {"name": "xr_layout", "type": "text"}


async def _add(client: TestClient, body: dict[str, Any]):
    return await client.post(PATH, json=body)


async def _added(client: TestClient, body: dict[str, Any]) -> dict[str, Any]:
    resp = await _add(client, body)
    assert resp.status == 200, await resp.text()
    return await resp.json()


def _stored(store: Any) -> dict[str, Any]:
    [record] = store.records.values()
    return parse_package(record.content)


def _in_force() -> dict[str, Any]:
    pinned = _instance().pinned_package(_instance().read(SLUG))
    assert pinned is not None
    return parse_package(pinned.content)


def _sentence(code: str, **fill: Any) -> str:
    return add_field.REFUSALS[code][1].format(**fill)


async def _refused(client: TestClient, body: dict[str, Any], code: str, **fill: Any) -> str:
    """Assert *body* is refused as *code* with its fixed sentence; return the reply text."""
    resp = await _add(client, body)
    text = await resp.text()
    assert json.loads(text) == {"error": _sentence(code, **fill), "code": code}, text
    assert resp.status == add_field.REFUSALS[code][0]
    return text


# --------------------------------------------------------------------------- #
# G1, G2: the page is the source's, whole
# --------------------------------------------------------------------------- #


async def test_g1_the_draft_keeps_the_view_and_theme_byte_for_byte(monkeypatch, store):
    """G1. A 200 kB view and the theme come back as they were; only the model grew.

    Red when ``_compose`` builds the package from anything but the source page (for
    one, dropping ``view`` from it).
    """
    body = _package_body()
    body["view"]["html"] += "\n<!-- " + "é\u2028x" * 50_000 + " -->"
    body["theme"] = {"tokens": {"--accent": "oklch(0.7 0.15 150)"}, "css": "p{margin:0}"}
    assert len(body["view"]["html"].encode("utf-8")) > 200_000
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, body)
        before = _in_force()
        reply = await _added(client, LAYOUT)
    after = _stored(store)
    assert after["view"]["html"] == before["view"]["html"] == body["view"]["html"]
    assert after["theme"] == before["theme"]
    assert {k: v for k, v in after.items() if k != "model"} == {
        k: v for k, v in before.items() if k != "model"
    }
    assert reply["onto"] == "live" and reply["note"] == add_field.ONTO["live"]


async def test_g2_every_old_field_keeps_its_type_and_source_and_the_new_one_is_there(
    monkeypatch, store
):
    """G2. The old fields are untouched and the new one is the Bot's own to write.

    Red when the new field's ``source`` is taken from the body or left out, or when
    ``types`` is rebuilt instead of copied.
    """
    body = _package_body()
    body["model"]["types"]["work_rows"] = {
        "type": "array",
        "source": {"fold": "work", "path": "items"},
    }
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, body)
        before = _in_force()["model"]["types"]
        reply = await _added(
            client, {"name": "xr_layout", "type": "text", "label": "Room", "description": "Parts"}
        )
    after = _stored(store)["model"]["types"]
    assert {k: v for k, v in after.items() if k != "xr_layout"} == before
    assert after["xr_layout"] == {
        "type": "text",
        "source": {"agentic": True},
        "label": "Room",
        "description": "Parts",
    }
    assert reply["added"] == {"field": "xr_layout", "type": "text"}
    assert reply["saved"]["fields"] == len(before) + 1


# --------------------------------------------------------------------------- #
# G3, G5: a draft, and what a rollback leaves
# --------------------------------------------------------------------------- #


async def test_g3_nothing_is_live_until_apply(monkeypatch, store):
    """G3. The page in force and its version do not move; ``dashboard_apply`` moves them.

    Red when the route applies what it saved (a call of ``_apply_own_package``).
    """
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        version = _instance().read(SLUG).instance_version
        live = _in_force()
        reply = await _added(client, LAYOUT)
        assert _instance().read(SLUG).instance_version == version
        assert _in_force() == live and "xr_layout" not in live["model"]["types"]
        assert reply["next"] == add_field.NEXT and "dashboard_apply" in reply["next"]
        assert (await _apply(client)).status == 200
    assert _instance().read(SLUG).instance_version == version + 1
    assert "xr_layout" in _in_force()["model"]["types"]


async def test_g5_after_a_rollback_the_field_is_gone_and_a_write_to_it_is_refused(
    monkeypatch, tmp_path
):
    """G5. Rolled back, the page in force has no such field and the write check refuses it.

    And the next add (D186): ``instance.rollback`` commits an instance version and
    writes no artifact, so the STORED page is the one he left. It is not the source: the
    page in force is. So the same name lands again, on a copy of the page in force. That
    stored page then has the bytes of the page he left and is a new page, written since:
    a draft, and the next field lands beside the first. Red when ``_compose`` takes the
    stored page he left, and red when equal bytes written anew are taken for it.
    """
    from kiro_crew import artifacts, dashboard_agentic
    from kiro_crew import dashboard_package as reader
    from kiro_crew.artifacts import ArtifactStore

    real = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: real)
    monkeypatch.setattr(artifacts, "get_default_store", lambda: real)
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        await _added(client, LAYOUT)
        assert (await _apply(client)).status == 200
        model = reader.read_package_model(SLUG).model
        assert model is not None
        dashboard_agentic.check_write(model, "xr_layout", "{}", {})
        back = await client.post("/api/agent-panel/dashboard/rollback", json={"to_version": 1})
        assert back.status == 200, await back.text()

        model = reader.read_package_model(SLUG).model
        assert model is not None and "xr_layout" not in model.manifest.fields
        with pytest.raises(dashboard_agentic.WriteRefused):
            dashboard_agentic.check_write(model, "xr_layout", "{}", {})
        assert "xr_layout" not in _in_force()["model"]["types"]

        again = await _added(client, LAYOUT)
        reply = await _added(client, {"name": "pace", "type": "number"})
    assert (again["onto"], reply["onto"]) == ("live", "draft")
    [record] = real.list(kind=gate.DASHBOARD_KIND)
    assert set(parse_package(real.get(record.slug).content)["model"]["types"]) == {
        "open_prs",
        "headline",
        "xr_layout",
        "pace",
    }


# --------------------------------------------------------------------------- #
# G4: every refusal, with its sentence
# --------------------------------------------------------------------------- #


async def _live(client: TestClient) -> None:
    await _save_and_apply(client, _package_body())


async def _nothing(_client: TestClient) -> None:
    return None


def _full(n: int = gate.MAX_MODEL_FIELDS) -> dict[str, Any]:
    body = _package_body()
    for i in range(n - len(body["model"]["types"])):
        body["model"]["types"][f"extra_{i}"] = {"type": "number", "source": {"agentic": True}}
    assert len(body["model"]["types"]) == n
    return body


#: code -> (the page there is before the call, the body, what fills the sentence).
BODY_REFUSALS: dict[str, tuple[Any, dict[str, Any], dict[str, Any]]] = {
    "validation_error": (_live, {**LAYOUT, MARK: 1}, {}),
    "bad_field_name": (_live, {"name": f"Open {MARK}", "type": "text"}, {}),
    "unsafe_declared_name": (_live, {"name": TOKEN_NAME, "type": "text"}, {}),
    "unknown_type": (
        _live,
        {"name": "pace", "type": MARK},
        {"types": "array, bool, number, object, text, timestamp"},
    ),
    "type_needs_more": (_live, {"name": "pace", "type": "enum"}, {}),
    "bad_label": (_live, {**LAYOUT, "label": MARK * 14}, {"limit": 120}),
    "bad_description": (_live, {**LAYOUT, "description": f"{MARK}\n{MARK}"}, {"limit": 120}),
    "no_page": (_nothing, LAYOUT, {}),
    "field_declared": (_live, {"name": "open_prs", "type": "text"}, {"name": "open_prs"}),
}


@pytest.mark.parametrize("code", sorted(BODY_REFUSALS))
async def test_g4_a_refusal_is_its_own_fixed_sentence_and_writes_nothing(
    code, monkeypatch, store, caplog
):
    """G4. Each refusal a body alone can earn: status, code, sentence, nothing written.

    Red, per code, when its check is taken out of ``_vetted`` or ``_compose`` (the call
    then answers 200 or another code), and when a sentence carries the caller's text.
    """
    before, body, fill = BODY_REFUSALS[code]
    caplog.set_level(logging.DEBUG)
    async with _client(monkeypatch) as client:
        await before(client)
        contents = {slug: rec.content for slug, rec in store.records.items()}
        updates = list(store.updates)
        text = await _refused(client, body, code, **fill)
    assert {slug: rec.content for slug, rec in store.records.items()} == contents
    assert store.updates == updates
    assert MARK not in text and TOKEN_NAME not in text
    assert MARK not in caplog.text and TOKEN_NAME not in caplog.text


@pytest.mark.parametrize(
    "body",
    [
        {"type": "text"},
        {"name": "pace"},
        {"name": 7, "type": "text"},
        {**LAYOUT, "source": {"fold": "work", "path": "items"}},
        {**LAYOUT, "bound_to": "crewmate:other-crew"},
        {**LAYOUT, "view": {"html": "<p></p>"}},
        {**LAYOUT, "choices": ["a"]},
    ],
)
async def test_g4_a_body_outside_the_closed_schema_is_refused(body, monkeypatch, store):
    """G4, case "a body with unknown keys". ``source``, ``bound_to`` and a page are not taken.

    Red when the handler stops validating against ``DASHBOARD_ADD_FIELD_SCHEMA``, or the
    schema gains one of these keys.
    """
    async with _client(monkeypatch) as client:
        await _live(client)
        before = _stored(store)
        await _refused(client, body, "validation_error")
    assert _stored(store) == before


@pytest.mark.parametrize("name", ["9lives", "Open", "a-b", "a" * 65, "", "naïve", "a b"])
async def test_g4_a_name_the_gate_would_not_take_is_refused_before_the_page_is_read(
    name, monkeypatch, store
):
    """G4. The gate's own name rule, asked before the lock. A blank name fails the schema."""
    async with _client(monkeypatch) as client:
        await _refused(
            client,
            {"name": name, "type": "text"},
            "bad_field_name" if name else "validation_error",
        )


async def test_g4_a_root_session_is_answered_as_the_save_answers_it(monkeypatch, store):
    """G4, case "a root session's page". The save's sentence and code, word for word.

    Red when the ``not crew_name`` refusal is removed (the lock then raises and the
    answer is 503) or reworded.
    """
    session = _instance().session_instance_key("chat-7-17910")
    async with _client(monkeypatch, resolved=(session, "", "chat-7-17910")) as client:
        saved = await _save(client, _package_body())
        added = await _add(client, LAYOUT)
        assert (added.status, await added.json()) == (saved.status, await saved.json())
        assert (await added.json())["code"] == "session_package_unsupported"
    assert store.records == {}


async def test_g4_a_page_already_at_the_most_fields_is_refused(monkeypatch, store):
    """G4, cases "a page with 64 fields" and "the draft holds 64 fields"."""
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _full())
        await _refused(client, LAYOUT, "model_full", count=64, most=64)
        # A draft at the most, over a smaller page in force.
        body = _full(63)
        _flip(body)
        await _save_and_apply(client, body)
        await _added(client, {"name": "last_one", "type": "bool"})
        await _refused(client, LAYOUT, "model_full", count=64, most=64)
    assert "xr_layout" not in _stored(store)["model"]["types"]


async def test_g4_a_page_the_add_takes_over_the_size_cap_is_refused(monkeypatch, store):
    """G4 and M180. With the field the stored form is over the cap; without it, under.

    The real cap is above what one page can reach (twice the view cap), so the cap is
    lowered to the stored page's own size. Red when the size is not measured (the
    answer is then ``add_refused``).
    """
    async with _client(monkeypatch) as client:
        await _live(client)
        [record] = store.records.values()
        size = len(record.content.encode("utf-8"))
        monkeypatch.setattr(gate, "MAX_PACKAGE_BYTES", size)
        await _refused(client, LAYOUT, "page_too_large", limit=size)
    assert "xr_layout" not in _stored(store)["model"]["types"]


async def test_g4_stored_widgets_with_the_preview_off_are_refused(monkeypatch, store):
    """G4 and M180. The page holds widgets and the preview is off: its own sentence.

    Red when the ``widgets_preview.widgets_enabled`` check leaves ``_compose``.
    """
    body = _package_body()
    body["widgets"] = [
        {
            "id": "prs",
            "title": "PRs",
            "sizes": ["small"],
            "blocks": [{"type": "metric", "label": "Open PRs", "field": "open_prs"}],
        }
    ]
    monkeypatch.setattr(widgets_preview, "widgets_enabled", lambda: True)
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, body)
        monkeypatch.setattr(widgets_preview, "widgets_enabled", lambda: False)
        await _refused(client, LAYOUT, "stored_widgets_preview_off")
        assert "xr_layout" not in _stored(store)["model"]["types"]
        monkeypatch.setattr(widgets_preview, "widgets_enabled", lambda: True)
        await _added(client, LAYOUT)
    assert [w["id"] for w in _stored(store)["widgets"]] == ["prs"]


@pytest.mark.parametrize("which", ["live", "draft"])
async def test_g4_a_page_that_fails_todays_gate_is_refused_and_not_blamed_on_the_bot(
    which, monkeypatch, store
):
    """G4 and M180. The source page was valid when stored and the gate moved since.

    ``text`` is taken out of the catalog after the page is stored. Both sources: the
    page in force, and a draft. Red when the source is not parsed before the add.
    """
    async with _client(monkeypatch) as client:
        await _live(client)
        if which == "draft":
            assert (await _save(client, _full(5))).status == 200
        before = _stored(store)
        monkeypatch.setattr(
            gate, "_STUB_DATA_TYPES", tuple(t for t in gate._STUB_DATA_TYPES if t.name != "text")
        )
        text = await _refused(client, {"name": "pace", "type": "number"}, "stored_page_invalid")
        monkeypatch.undo()
    assert "not your doing" in text
    assert _stored(store) == before


async def test_g4_a_page_that_cannot_be_read_is_refused(monkeypatch, store):
    """G4, case "the stored page cannot be read": the record, the pin, the artifact.

    Red when a read failure in ``_compose`` is let through as a 500, or answered with
    the failure's own text.
    """
    instance = _instance()
    async with _client(monkeypatch) as client:
        await _live(client)
        [record] = store.records.values()
        before = record.content

        store.unreadable.add(record.slug)
        text = await _refused(client, LAYOUT, "page_unreadable")
        assert record.slug not in text
        store.unreadable.clear()

        def _down(_slug: str) -> Any:
            raise OSError(f"{MARK} cannot be opened")

        with monkeypatch.context() as patched:
            patched.setattr(instance, "read", _down)
            await _refused(client, LAYOUT, "page_unreadable")

        # The pin is there and its snapshot is not: which page is live cannot be said.
        with monkeypatch.context() as patched:
            patched.setattr(instance, "pinned_package", lambda _record: None)
            await _refused(client, LAYOUT, "page_unreadable")
    assert record.content == before


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (ArtifactValidationError(f"the gate says {MARK}"), "add_refused"),
        (RuntimeError(f"the disk says {MARK}"), "save_failed"),
    ],
)
async def test_g4_a_write_that_fails_is_answered_in_fixed_words(
    failure, code, monkeypatch, store, caplog
):
    """G4. A refusal or a failure of the WRITE: fixed words, and the cause's text nowhere.

    Red when the handler answers with ``str(exc)`` as the save does, or logs it.
    """
    caplog.set_level(logging.DEBUG)

    def _fails(*_a: Any, **_k: Any) -> Any:
        raise failure

    async with _client(monkeypatch) as client:
        await _live(client)
        monkeypatch.setattr(store, "update", _fails)
        text = await _refused(client, LAYOUT, code)
    assert MARK not in text and MARK not in caplog.text


def test_g4_every_refusal_the_route_holds_has_a_row_here():
    """G4. The table of refusals and the tests agree; none is left without a row."""
    elsewhere = {
        "session_package_unsupported",
        "model_full",
        "page_too_large",
        "stored_widgets_preview_off",
        "stored_page_invalid",
        "page_unreadable",
        "add_refused",
        "page_changed",
        "template_live",
        "history_before_pins",
        "history_not_kept",
        "save_failed",
    }
    assert set(BODY_REFUSALS) | elsewhere == set(add_field.REFUSALS)
    assert not set(BODY_REFUSALS) & elsewhere
    for status, sentence in add_field.REFUSALS.values():
        assert status in (400, 403, 409, 503) and sentence


# --------------------------------------------------------------------------- #
# G6: the gate. The caller is the session's own binding.
# --------------------------------------------------------------------------- #


async def test_g6_the_route_answers_403_without_the_internal_secret(vetted, store):
    """G6. No door: without the secret the caller gate refuses, under its own audit name.

    The real ``_resolve_dashboard_caller`` runs here. Red when the handler stops calling
    it first, or the route moves off the strict-internal prefix.
    """
    from kiro_crew.dashboard import server
    from kiro_crew.sel import sel

    seen: list[dict[str, Any]] = []
    real = sel().log_api_access

    def _audit(**kw: Any) -> Any:
        seen.append(kw)
        return real(**kw)

    with pytest.MonkeyPatch.context() as patched:
        patched.setattr(sel(), "log_api_access", _audit)
        async with _gated_client(internal=False) as client:
            resp = await client.post(
                PATH, json=LAYOUT, headers={"X-Session-Key": "dashboard:chat-1"}
            )
            assert resp.status == 403
            assert (await resp.json())["code"] == "internal_secret_required"
            # The save's answer to the same caller, to the byte.
            saved = await client.post(
                "/api/agent-panel/dashboard/save",
                json=_package_body(),
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert (saved.status, await saved.text()) == (resp.status, await resp.text())
    assert store.records == {}
    assert [(kw["operation"], kw["outcome"]) for kw in seen] == [
        ("dashboard_add_field", "denied"),
        ("dashboard_save", "denied"),
    ]
    assert any(PATH.startswith(prefix) for prefix in server._STRICT_INTERNAL_API_PATHS)
    assert not [p for p in server._STRICT_INTERNAL_API_PATHS if "add-field" in p]


async def test_g6_the_page_written_is_the_sessions_own_whatever_the_body_says(vetted, store):
    """Ruling 6. The crew comes from the session's binding; the body cannot name one.

    The real gate resolves ``dashboard:chat-1`` to the crew it is bound to. Red when the
    binding is read from the body.
    """
    headers = {"X-Session-Key": "dashboard:chat-1"}
    async with _gated_client() as client:
        saved = await client.post(
            "/api/agent-panel/dashboard/save", json=_package_body(), headers=headers
        )
        assert saved.status == 200, await saved.text()
        applied = await client.post("/api/agent-panel/dashboard/apply", json={}, headers=headers)
        assert applied.status == 200, await applied.text()
        for extra in ({"slug": "other-crew"}, {"crew": "other-crew"}, {"member": "other-crew"}):
            refused = await client.post(PATH, json={**LAYOUT, **extra}, headers=headers)
            assert (await refused.json())["code"] == "validation_error"
        added = await client.post(PATH, json=LAYOUT, headers=headers)
        assert added.status == 200, await added.text()
    page = _stored(store)
    assert page["bound_to"] == f"crewmate:{SLUG}"
    assert "xr_layout" in page["model"]["types"]


def test_g6_both_route_tables_serve_it_with_the_one_handler():
    """The panel's own table and the MCP boot table register the same path and handler."""
    from aiohttp import web

    app = web.Application()
    routes.register_agent_panel_routes(app)
    [route] = [r for r in app.router.routes() if r.resource.canonical == PATH]
    assert (route.method, route.handler) == ("POST", routes.api_dashboard_add_field)
    table = (Path(__file__).parent / "fixtures" / "dashboard_mcp_route_table.txt").read_text(
        encoding="utf-8"
    )
    assert f"POST {PATH} api_dashboard_add_field\n" in table


# --------------------------------------------------------------------------- #
# G7, G8, G9: one name twice, two calls, a draft
# --------------------------------------------------------------------------- #


async def test_g7_the_same_name_twice_is_refused_the_second_time(monkeypatch, store):
    """G7. With no apply between, and again after one. Nothing is written twice.

    Red when the ``name in types`` check is removed (the second call answers 200).
    """
    async with _client(monkeypatch) as client:
        await _live(client)
        await _added(client, LAYOUT)
        once = _stored(store)
        await _refused(
            client, {"name": "xr_layout", "type": "number"}, "field_declared", name="xr_layout"
        )
        assert (await _apply(client)).status == 200
        await _refused(client, LAYOUT, "field_declared", name="xr_layout")
    assert _stored(store) == once


async def test_g8_two_adds_one_after_the_other_with_no_apply_between_both_land(monkeypatch, store):
    """G8. The second add's source is the draft the first left, so both are there.

    Red when the source is the page in force while a draft exists: the second add
    would then write a page without the first field.
    """
    async with _client(monkeypatch) as client:
        await _live(client)
        first = await _added(client, LAYOUT)
        second = await _added(client, {"name": "pace", "type": "number"})
    assert (first["onto"], second["onto"]) == ("live", "draft")
    assert second["note"] == add_field.ONTO["draft"]
    assert set(_stored(store)["model"]["types"]) == {"open_prs", "headline", "xr_layout", "pace"}


class _LockWatch:
    """Wraps the save lock: who is inside, and a signal when a second caller arrives."""

    def __init__(self, monkeypatch: Any) -> None:
        self.real = _instance().package_save_lock
        self.inside = threading.local()
        self.arrivals = 0
        self.guard = threading.Lock()
        monkeypatch.setattr(_instance(), "package_save_lock", self.lock)

    @contextmanager
    def lock(self, slug: str):
        with self.guard:
            self.arrivals += 1
        with self.real(slug):
            self.inside.held = True
            try:
                yield
            finally:
                self.inside.held = False

    def held(self) -> bool:
        return bool(getattr(self.inside, "held", False))


async def test_g8_two_adds_at_once_with_two_names_both_land(monkeypatch, store):
    """G8 and M179. The first add is held INSIDE the lock until the second has arrived.

    The page is read inside the lock, so the second reads what the first wrote. Red
    when the read moves out of the lock (``compose`` called before
    ``package_save_lock``): both then read the same page and one field is lost.
    """
    watch = _LockWatch(monkeypatch)
    release = threading.Event()
    real_update = store.update
    reads_in_lock: list[bool] = []
    real_compose = add_field._compose

    def _compose(*a: Any, **k: Any) -> Any:
        reads_in_lock.append(watch.held())
        return real_compose(*a, **k)

    def _held_update(*a: Any, **k: Any) -> Any:
        assert watch.held(), "the write is outside the save lock"
        assert release.wait(timeout=default_timeout()), "the second add never arrived"
        return real_update(*a, **k)

    async with _client(monkeypatch) as client:
        await _live(client)
        watch.arrivals = 0
        monkeypatch.setattr(routes, "_compose", _compose)
        monkeypatch.setattr(store, "update", _held_update)
        one = asyncio.ensure_future(_add(client, LAYOUT))
        two = asyncio.ensure_future(_add(client, {"name": "pace", "type": "number"}))
        # Both have asked for the lock: one holds it in its write, one waits.
        await async_wait_until(lambda: watch.arrivals == 2)
        release.set()
        replies = [await (await call).json() for call in (one, two)]
    assert [r.get("ok") for r in replies] == [True, True], replies
    assert sorted(r["onto"] for r in replies) == ["draft", "live"]
    assert reads_in_lock == [True, True]
    assert set(_stored(store)["model"]["types"]) == {"open_prs", "headline", "xr_layout", "pace"}


@pytest.mark.parametrize("first", ["save", "add"])
async def test_g8_a_save_and_an_add_of_one_bot_run_one_after_the_other(first, monkeypatch, store):
    """Cases "a call while a save is in flight" and "a save while an add is in flight".

    Whichever holds the lock finishes whole before the other reads. Save first: the add
    lands on the page the save wrote. Add first: the save replaces the page, as a save
    does. Nothing is half of each. Red when the add reads outside the lock.
    """
    watch = _LockWatch(monkeypatch)
    release = threading.Event()
    real_update = store.update
    order: list[str] = []

    def _held_update(*a: Any, **k: Any) -> Any:
        if not order:
            order.append("held")
            assert release.wait(timeout=default_timeout()), "the other call never arrived"
        return real_update(*a, **k)

    new_page = _full(5)
    _flip(new_page)
    async with _client(monkeypatch) as client:
        await _live(client)
        watch.arrivals = 0
        monkeypatch.setattr(store, "update", _held_update)
        calls = {
            "save": lambda: _save(client, new_page),
            "add": lambda: _add(client, LAYOUT),
        }
        second = "add" if first == "save" else "save"
        a = asyncio.ensure_future(calls[first]())
        await async_wait_until(lambda: order == ["held"])
        b = asyncio.ensure_future(calls[second]())
        await async_wait_until(lambda: watch.arrivals == 2)
        release.set()
        assert (await a).status == 200 and (await b).status == 200
    page = _stored(store)
    assert page["view"]["html"] == new_page["view"]["html"]
    if first == "save":
        assert set(page["model"]["types"]) == set(new_page["model"]["types"]) | {"xr_layout"}
    else:
        assert set(page["model"]["types"]) == set(new_page["model"]["types"])


async def test_g9_a_saved_page_not_applied_is_the_source_whole(monkeypatch, store):
    """G9 and M178. A draft and a page in force with different views and different fields.

    The result is the draft's view byte for byte, the draft's theme, the draft's fields
    plus the new one, and nothing of the page in force; the reply says so. Red when any
    part of the page comes from the page in force.
    """
    live = _package_body()
    draft = {
        "model": {"types": {"pace": {"type": "number", "source": {"agentic": True}}}},
        "view": {"html": '<h1>draft</h1>\n<p data-dashboard-field="pace"></p>'},
        "theme": {"tokens": {"--ink": "oklch(0.2 0 0)"}},
    }
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, live)
        in_force = _in_force()
        assert (await _save(client, draft)).status == 200
        saved_draft = _stored(store)
        reply = await _added(client, LAYOUT)
        assert _in_force() == in_force, "the page in force moved"
    page = _stored(store)
    assert page["view"]["html"] == draft["view"]["html"] == saved_draft["view"]["html"]
    assert page["theme"] == saved_draft["theme"]
    assert set(page["model"]["types"]) == {"pace", "xr_layout"}
    assert page["model"]["types"]["pace"] == saved_draft["model"]["types"]["pace"]
    as_text = json.dumps(page)
    for part in ("open_prs", "headline", "--accent", "Open PRs"):
        assert part not in as_text, part
    assert (reply["onto"], reply["note"]) == ("draft", add_field.ONTO["draft"])
    assert "was not live when it was read" in reply["note"] and "whole page" in reply["note"]


async def test_g9_a_draft_that_already_declares_the_name_is_refused(monkeypatch, store):
    """Case "the draft already declares the name", while the page in force does not."""
    draft = _package_body()
    draft["model"]["types"]["xr_layout"] = {"type": "text", "source": {"agentic": True}}
    _flip(draft)
    async with _client(monkeypatch) as client:
        await _live(client)
        assert (await _save(client, draft)).status == 200
        before = _stored(store)
        await _refused(client, LAYOUT, "field_declared", name="xr_layout")
    assert _stored(store) == before


async def test_a_first_page_only_saved_takes_a_field_and_no_page_takes_none(monkeypatch, store):
    """A page saved and never applied is the source; with no page at all the call is refused."""
    async with _client(monkeypatch) as client:
        await _refused(client, LAYOUT, "no_page")
        assert (await _save(client, _package_body())).status == 200
        reply = await _added(client, LAYOUT)
    assert reply["onto"] == "draft"
    assert _instance().read(SLUG).instance_version == 0


# --------------------------------------------------------------------------- #
# M210 (D186): a page he left is not the source. M213: the source is the caller's.
# --------------------------------------------------------------------------- #

_PAGE_B = {
    "model": {"types": {"b_only": {"type": "number", "source": {"agentic": True}}}},
    "view": {"html": '<h1>page B</h1>\n<p data-dashboard-field="b_only"></p>'},
    "theme": {"tokens": {"--ink": "oklch(0.2 0 0)"}},
}


async def _rollback(client: TestClient, to_version: int) -> None:
    back = await client.post("/api/agent-panel/dashboard/rollback", json={"to_version": to_version})
    assert back.status == 200, await back.text()


async def _template(client: TestClient) -> None:
    resp = await client.post(
        "/api/agent-panel/dashboard/apply", json={"template_id": "project-report"}
    )
    assert resp.status == 200, await resp.text()


async def test_m210_save_add_add_apply_puts_both_fields_live(monkeypatch, store):
    """Walk 1. A page only saved is a true draft; each add lands on it; one apply puts all live."""
    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        first = await _added(client, LAYOUT)
        second = await _added(client, {"name": "pace", "type": "number"})
        assert (await _apply(client)).status == 200
    assert (first["onto"], second["onto"]) == ("draft", "draft")
    assert set(_in_force()["model"]["types"]) == {"open_prs", "headline", "xr_layout", "pace"}


async def test_m210_after_a_rollback_one_add_and_one_apply_do_not_bring_back_the_page_he_left(
    monkeypatch, store
):
    """Walk 4, the reviewer's P3. Page A live, page B saved and applied, a rollback to A,
    an add, an apply: A with the field is live. Nothing of B is.

    Red when the stored page he left is the source (``digest in applied.pins`` not asked).
    """
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        await _save_and_apply(client, _PAGE_B)
        await _rollback(client, 1)
        page_a = _in_force()
        reply = await _added(client, LAYOUT)
        assert _in_force() == page_a
        assert (await _apply(client)).status == 200
    live = _in_force()
    assert (reply["onto"], reply["note"]) == ("live", add_field.ONTO["live"])
    assert live["view"] == page_a["view"] and live["theme"] == page_a["theme"]
    assert set(live["model"]["types"]) == {"open_prs", "headline", "xr_layout"}
    assert "b_only" not in json.dumps(live) and "page B" not in json.dumps(live)


async def test_m210_with_a_shipped_template_in_force_the_page_he_left_takes_no_field(
    monkeypatch, store
):
    """Walk 5, the reviewer's P3b. A page saved and applied, then a shipped template: the
    add is refused, in its own sentence, and nothing is written.

    Red when the stored page is taken as a draft.
    """
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        await _template(client)
        before = _stored(store)
        updates = list(store.updates)
        await _refused(client, LAYOUT, "template_live")
    assert _stored(store) == before and store.updates == updates


async def test_m210_a_record_from_before_pins_is_not_guessed_at(monkeypatch, store):
    """Where the record cannot tell (a): no ``package`` key, and the stored version is one
    a person cut. Refused, nothing written; no fall back to the stored page or to the page
    in force. The way on is the Bot's own: a save cuts a version no person cut.

    Red when the add lands instead (the ``history_before_pins`` refusal taken out).
    """
    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        [record] = store.records.values()
        # A person cut this version (the older rule's own marker, ``applied_package_version``).
        record.events = [{"by": "user", "type": "edited", "version": record.version}]
        assert instance_applied.applied_pages(SLUG).keyed is False
        before = record.content
        await _refused(client, LAYOUT, "history_before_pins")
        assert record.content == before
        again = _package_body()
        _flip(again)
        assert (await _save(client, again)).status == 200
        reply = await _added(client, LAYOUT)
    assert reply["onto"] == "draft"
    assert _stored(store)["view"]["html"] == again["view"]["html"]


async def test_m210_a_stored_page_older_than_the_kept_versions_is_not_guessed_at(
    monkeypatch, store
):
    """Where the record cannot tell (b): the version that may have pinned the stored page
    was pruned. Refused, nothing written. And the edge: a page written AT the oldest kept
    version's time is on the tellable side (every version that could pin it is kept), one
    written a millisecond before is not. The way on: a save.

    Red when the add lands instead (the ``history_not_kept`` refusal taken out).
    """
    from datetime import datetime, timezone

    instance = _instance()
    monkeypatch.setattr(instance, "MAX_RETAINED_VERSIONS", 2)
    iso = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()  # noqa: E731
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        # Three versions that pin no page of his: version 1, which pinned it, is pruned.
        for _ in range(3):
            await _template(client)
        assert instance.versions(SLUG)[0] > 1, "no version was pruned: the row made no case"
        said = instance_applied.applied_pages(SLUG)
        [record] = store.records.values()
        assert said.tellable_since_ms > 0 and add_field._written_ms(record) < said.tellable_since_ms
        before = record.content
        await _refused(client, LAYOUT, "history_not_kept")
        assert record.content == before
        # The edge, by the comparison chosen: written_ms >= the oldest kept version's time.
        record.updated_at = iso(said.tellable_since_ms - 1)
        await _refused(client, LAYOUT, "history_not_kept")
        record.updated_at = iso(said.tellable_since_ms)
        edge = await client.post(PATH, json=LAYOUT)
        assert (await edge.json()).get("code") != "history_not_kept", await edge.text()
        # The way on: a save of the page he wants, then the add.
        record.content = before
        record.updated_at = iso(said.tellable_since_ms - 1)
        assert (await _save(client, _package_body())).status == 200
        reply = await _added(client, {"name": "pace", "type": "number"})
    assert reply["ok"] is True


async def test_m213_the_source_page_is_the_callers_own(monkeypatch, store):
    """M213, the reviewer's break X2. A record the binding lookup hands back that is bound
    to another crew is not a source: refused, nothing written.

    Red when the ``bound_to`` check leaves ``_compose``.
    """
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        [record] = store.records.values()
        other = _stored(store)
        other["bound_to"] = "crewmate:other-crew"
        record.content = gate.canonical_package_content(json.dumps(other))
        before = record.content
        monkeypatch.setattr(gate, "resolve_bound_slug", lambda *_a, **_k: record.slug)
        await _refused(client, LAYOUT, "page_unreadable")
    assert record.content == before


async def test_m213_a_pinned_page_with_no_stored_artifact_is_written_anew(monkeypatch, store):
    """M213. His page is in force and its artifact is gone: the source is the page in
    force, whole, and a new record holds it with the field. Bound to the caller."""
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        live = _in_force()
        store.records.clear()
        reply = await _added(client, LAYOUT)
    page = _stored(store)
    assert reply["onto"] == "live" and page["bound_to"] == f"crewmate:{SLUG}"
    assert page["view"] == live["view"] and page["theme"] == live["theme"]
    assert set(page["model"]["types"]) == set(live["model"]["types"]) | {"xr_layout"}


# --------------------------------------------------------------------------- #
# M209: the write is fenced on the read the page was built from
# --------------------------------------------------------------------------- #


def _other_writer(real: Any) -> dict[str, Any]:
    """Another writer of the same page, as the artifact route writes: its own fenced read,
    then its page with a view and a field of its own (``artifacts_parts/create_update.py``)."""
    [record] = real.list(kind=gate.DASHBOARD_KIND)
    read = real.get(record.slug, assign_token=True)
    page = parse_package(read.content)
    page["view"] = {"html": page["view"]["html"] + "\n<p>the other writer's</p>"}
    page["model"]["types"]["others_field"] = {"type": "bool", "source": {"agentic": True}}
    real.update(
        record.slug,
        content=json.dumps(page),
        actor="user",
        snapshot=True,
        snapshot_token=read.content_token,
    )
    return page


@pytest.mark.parametrize("stores", ["real", "stand-in"])
async def test_m209_a_write_by_another_route_after_the_adds_read_is_not_overwritten(
    stores, monkeypatch, tmp_path, store
):
    """M209, the reviewer's P1. The add reads the page; another writer writes it with its
    own fenced read; the add's write comes. Nothing of the other writer's is lost: the
    add is refused as ``page_changed`` and writes nothing. On the real store, and on the
    units' own, which reads the token as the real one does (M214).

    Red when the write is fenced on a second read (``_write_composed`` reading the
    record again for its token): the add then answers ``ok`` and the other writer's
    view and field are gone.
    """
    from kiro_crew import artifacts
    from kiro_crew.artifacts import ArtifactStore

    real: Any = store
    if stores == "real":
        real = ArtifactStore(tmp_path / "artifacts")
        monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: real)
        monkeypatch.setattr(artifacts, "get_default_store", lambda: real)
    compose = add_field._compose
    wrote: list[dict[str, Any]] = []

    def _read_then_the_other_writes(*a: Any, **k: Any) -> Any:
        built = compose(*a, **k)
        wrote.append(_other_writer(real))
        return built

    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        monkeypatch.setattr(routes, "_compose", _read_then_the_other_writes)
        text = await _refused(client, LAYOUT, "page_changed")
        monkeypatch.setattr(routes, "_compose", compose)
        [record] = real.list(kind=gate.DASHBOARD_KIND)
        stored = parse_package(real.get(record.slug).content)
        # The other writer's page stands whole: its view, its field, and no field of the add's.
        assert stored["view"]["html"].endswith("<p>the other writer's</p>")
        assert set(stored["model"]["types"]) == {"open_prs", "headline", "others_field"}
        assert "ask again" in text
        # Asked again, the add lands on the other writer's page and keeps all of it.
        reply = await _added(client, LAYOUT)
    assert len(wrote) == 1 and reply["onto"] == "draft"
    after = parse_package(real.get(record.slug).content)
    assert after["view"] == stored["view"]
    assert set(after["model"]["types"]) == {"open_prs", "headline", "others_field", "xr_layout"}


def test_m214_the_units_store_refuses_a_write_that_names_another_read():
    """M214. The stand-in store has the real store's fence: a write that names no read, or
    an older one, is refused and writes nothing; one that names the read it replaces lands."""
    from test_dashboard_save_tool import _Store

    from kiro_crew.artifact_store.model import ArtifactConflictError

    s = _Store()
    body = {"kind": "dashboard", "bound_to": f"crewmate:{SLUG}", **_package_body()}
    rec = s.create(name="x", content=json.dumps(body), kind=gate.DASHBOARD_KIND)
    first = s.get(rec.slug, assign_token=True)
    other = {**body, "view": {"html": body["view"]["html"] + "<p>2</p>"}}
    s.update(rec.slug, content=json.dumps(other), snapshot_token=first.content_token)
    assert s.get(rec.slug).content_token != first.content_token
    for named in (None, first.content_token):
        with pytest.raises(ArtifactConflictError):
            s.update(rec.slug, content=json.dumps(body), snapshot_token=named)
    assert parse_package(s.get(rec.slug).content)["view"] == other["view"]


# --------------------------------------------------------------------------- #
# Ruling 5: what the reply and the log carry. Ruling 8: the tool.
# --------------------------------------------------------------------------- #


async def test_the_reply_and_the_log_never_carry_the_callers_text(monkeypatch, store, caplog):
    """Ruling 5. A good add with a label and a description: neither comes back.

    Every value of the reply is a fixed word, the vetted name, the catalog's type, the
    gateway's package slug or a number. Red when the reply echoes ``label``.
    """
    caplog.set_level(logging.DEBUG)

    async def _checked(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"status": "failed", "errors": [{"message": f"page said {MARK}"}]}

    monkeypatch.setattr(routes, "_page_check", _checked)
    async with _client(monkeypatch) as client:
        await _live(client)
        resp = await _add(client, {**LAYOUT, "label": f"L {MARK}", "description": f"D {MARK}"})
        text = await resp.text()
    reply = json.loads(text)
    assert MARK not in text and MARK not in caplog.text
    [record] = store.records.values()
    assert reply == {
        "ok": True,
        "added": {"field": "xr_layout", "type": "text"},
        "onto": "live",
        "note": add_field.ONTO["live"],
        "saved": {"package": record.slug, "package_version": record.version, "fields": 3},
        "verification": "failed",
        "next": add_field.NEXT,
    }
    assert _stored(store)["model"]["types"]["xr_layout"]["label"] == f"L {MARK}"


def test_m211_the_reply_says_which_page_it_was_at_the_read_and_no_more():
    """M211. ``onto`` is decided at the read the page was built from; an apply or a rollback
    takes another lock and can come before the reply. So neither sentence says what is
    live NOW: each says what the page was when it was read. The tool says the same words.

    Red when a sentence says "your live page" or "not live yet".
    """
    assert set(add_field.ONTO) == {"draft", "live"}
    for sentence in add_field.ONTO.values():
        assert "when it was read" in sentence
        assert "your live page" not in sentence and "not live yet" not in sentence
    assert mcp_panel._ADDED_ONTO == add_field.ONTO


def test_m216_the_no_page_sentence_gives_no_advice():
    """M216. A Bot with no page of its own is told that, and is not told to compose one:
    the XR brief tells a Bot on a shipped page never to, for this."""
    sentence = add_field.REFUSALS["no_page"][1]
    assert sentence == "you have no page of your own to add a field to. Nothing was written"
    for word in ("dashboard_save", "dashboard_apply", "ompose"):
        assert word not in sentence
    assert "dashboard_save" not in add_field.REFUSALS["template_live"][1]


def _tool() -> dict[str, Any]:
    (tool,) = [t for t in mcp_panel._list_tools() if t["name"] == "dashboard_add_field"]
    return tool


def test_the_tool_takes_the_four_keys_and_the_gateways_types():
    """Ruling 4 and 8. A closed input schema; its types are the gateway's addable ones."""
    schema = _tool()["inputSchema"]
    assert set(schema["properties"]) == {"name", "type", "label", "description"}
    assert schema["required"] == ["name", "type"] and schema["additionalProperties"] is False
    assert schema["properties"]["type"]["enum"] == add_field.addable_types()
    assert tuple(add_field.addable_types()) == mcp_panel.ADD_FIELD_TYPES
    assert "enum" not in add_field.addable_types()
    assert mcp_panel._ADDED_ONTO == add_field.ONTO
    from kiro_crew.validation import DASHBOARD_ADD_FIELD_SCHEMA, MCP_PANEL_SCHEMAS

    assert MCP_PANEL_SCHEMAS["dashboard_add_field"] is DASHBOARD_ADD_FIELD_SCHEMA
    assert {f.name for f in DASHBOARD_ADD_FIELD_SCHEMA.fields} == set(schema["properties"])


def test_the_tool_text_says_what_the_route_does():
    """Ruling 8. The description is true of this commit: draft, source, apply, write."""
    text = _tool()["description"]
    for said in (
        "DRAFT",
        "ONE whole page",
        "your DRAFT when you have one",
        "a copy of your own live page",
        "A page you left, by a rollback or by applying a template after it, is not taken, unless",
        "renamed, described, tagged or saved again since it was last put live: then it reads as a draft and is taken",
        "dashboard_apply",
        "dashboard_write",
        '{"agentic": true}',
        "dashboard_save",
    ):
        assert said in text, said
    from kiro_crew.agent_parts.spec_grants import _MEMBER_PANEL_GRANTS

    assert "@kirocrew-panel/dashboard_add_field" in _MEMBER_PANEL_GRANTS


def test_the_tool_sends_only_the_four_and_renders_fixed_words(monkeypatch):
    """The MCP half: a fifth key is not forwarded, and the reply is the gateway's words."""
    sent: list[tuple[str, dict[str, Any]]] = []

    def _post(path: str, body: dict[str, Any], **_k: Any) -> dict[str, Any]:
        sent.append((path, body))
        return {
            "ok": True,
            "added": {"field": "xr_layout", "type": "text"},
            "onto": "draft",
            "note": add_field.ONTO["draft"],
            "saved": {"package": "p", "package_version": 4, "fields": 3},
            "verification": "passed",
            "next": add_field.NEXT,
        }

    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("sk", None))
    monkeypatch.setattr(mcp_panel, "_post", _post)
    out = mcp_panel._call_tool_inner(
        "dashboard_add_field",
        {**LAYOUT, "label": f"L {MARK}", "source": {"fold": "work"}, "bound_to": "x"},
    )
    assert sent == [(PATH, {**LAYOUT, "label": f"L {MARK}"})]
    assert MARK not in out
    assert out == (
        "Saved a DRAFT of your page with the new field `xr_layout` (text): 3 fields, "
        "package version 4. It is NOT live yet: " + add_field.ONTO["draft"] + ". "
        "The page check passed. Next: " + add_field.NEXT + "."
    )
    assert mcp_panel._call_tool_inner("dashboard_add_field", {"name": "x"}).startswith("Error:")
    assert len(sent) == 1


def test_the_tool_hands_back_a_refusal_whole(monkeypatch):
    """A refusal is the gateway's fixed sentence, which says what to do next."""
    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("sk", None))
    monkeypatch.setattr(
        mcp_panel,
        "_post",
        lambda *_a, **_k: {"error": add_field.REFUSALS["no_page"][1], "code": "no_page"},
    )
    out = mcp_panel._call_tool_inner("dashboard_add_field", dict(LAYOUT))
    assert out == "Error: " + add_field.REFUSALS["no_page"][1]


# Mark only the async tests: a module-wide ``pytestmark`` also lands on the sync ones.
for _name, _test in list(globals().items()):
    if _name.startswith("test_") and asyncio.iscoroutinefunction(_test):
        globals()[_name] = pytest.mark.asyncio(_test)
