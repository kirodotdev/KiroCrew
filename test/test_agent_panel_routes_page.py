"""The dashboard page's four routes over HTTP (templates, preview, apply, rollback), the catalog
listing the agent reads, and the history session lookup.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from agent_panel_routes_helpers import (  # noqa: F401 -- fixtures are used by name
    CREW,
    SLUG,
    _client,
    _crew_log_on,
    vetted_fixture,
)

from kiro_crew.crew_log import projection as crew_log_projection
from kiro_crew.dashboard.handlers import agent_panel as routes
from kiro_crew.dashboard_templates.instance import preview_url as instance_preview_url

pytestmark = pytest.mark.asyncio


# ------------------------------------- the page's four routes, over HTTP

TEMPLATES_PATH = "/api/agent-panel/dashboard/templates"
PREVIEW_PATH = "/api/agent-panel/dashboard/preview"
APPLY_PATH = "/api/agent-panel/dashboard/apply"
ROLLBACK_PATH = "/api/agent-panel/dashboard/rollback"


def _page_store(monkeypatch, **over: Any) -> SimpleNamespace:
    """A store standing in for the instance module, through this file's own seam.

    The same `sys.modules` + package-attribute pair `_adopt_dashboard` uses, and for
    the same reason: these four handlers are being tested, not the store underneath
    them, and driving the real store would bind every case here to the shipped
    catalogue's twelve directories and their sizes.

    ``InstanceRefused`` is a REAL exception class rather than a sentinel, because
    ``_instance_refusal`` separates a refusal from an unexpected failure with
    ``isinstance`` -- a stub that got that wrong would make a 400 case pass as a 503.
    """

    class InstanceRefused(Exception):
        pass

    class InstanceLocked(InstanceRefused):
        pass

    staged = {
        "template_id": "fixture-board",
        "template_version": 1,
        "title": "Fixture board",
        "fields": ["credits", "phase"],
        "html_bytes": 120,
        "staged_ms": 1,
    }
    store = SimpleNamespace(
        InstanceRefused=InstanceRefused,
        InstanceLocked=InstanceLocked,
        InstanceError=Exception,
        MAX_PREVIEW_AGE_MS=30 * 60 * 1000,
        read=lambda _slug: SimpleNamespace(template_id="fixture-board"),
        # `wire` takes the crew NAME and builds the link from it with the real
        # builder, so a route that forgets to pass the name fails here.
        stage_preview=lambda _slug, **_kw: SimpleNamespace(
            wire=lambda member: {**staged, "preview_url": instance_preview_url(member)}
        ),
        discard_preview=lambda _slug: None,
        apply_preview=lambda _slug, **_kw: SimpleNamespace(
            instance_version=5, template_id="fixture-board"
        ),
        adopt=lambda _slug, _template_id, **_kw: SimpleNamespace(
            instance_version=5, template_id="fixture-board"
        ),
        rollback=lambda _slug, _to, **_kw: SimpleNamespace(
            instance_version=7, template_id="fixture-board"
        ),
        STATE_LIVE="live",
    )
    for key, value in over.items():
        setattr(store, key, value)
    monkeypatch.setitem(sys.modules, "kiro_crew.dashboard_templates.instance", store)
    import kiro_crew.dashboard_templates as templates_pkg

    monkeypatch.setattr(templates_pkg, "instance", store, raising=False)
    return store


class TestTheTemplatesRouteOverHttp:
    async def test_it_answers_the_catalog_and_marks_the_current_template(
        self, vetted, monkeypatch
    ) -> None:
        _page_store(monkeypatch)
        monkeypatch.setattr(
            routes,
            "_templates_for_agent",
            lambda slug, query: {
                "templates": [{"id": "fixture-board", "current": True}],
                "current_template_id": "fixture-board",
                "query": query,
                "problems": [],
            },
        )
        async with _client() as c:
            resp = await c.get(
                f"{TEMPLATES_PATH}?query=cost", headers={"X-Session-Key": "dashboard:chat-1"}
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["current_template_id"] == "fixture-board"
        assert body["query"] == "cost", "the query did not reach the lister"

    async def test_the_query_is_bounded_before_it_reaches_the_lister(
        self, vetted, monkeypatch
    ) -> None:
        """200 characters, because the handler slices it rather than refusing.

        A query is a person's own words relayed by an agent, and a long one is a
        clumsy ask rather than an attack -- so it is cut and answered, not rejected.
        """
        seen: list[str] = []

        def _lister(_slug, query):
            seen.append(query)
            return {"templates": [], "current_template_id": "", "query": query, "problems": []}

        _page_store(monkeypatch)
        monkeypatch.setattr(routes, "_templates_for_agent", _lister)
        async with _client() as c:
            resp = await c.get(
                f"{TEMPLATES_PATH}?query={'x' * 400}",
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 200, await resp.text()
        assert seen and len(seen[0]) == 200, f"query reached the lister at {len(seen[0])} chars"

    async def test_a_registry_failure_is_a_refusal_and_not_a_500(self, vetted, monkeypatch) -> None:
        store = _page_store(monkeypatch)

        def _boom(_slug, _query):
            raise store.InstanceRefused("the registry cannot be read")

        monkeypatch.setattr(routes, "_templates_for_agent", _boom)
        async with _client() as c:
            resp = await c.get(TEMPLATES_PATH, headers={"X-Session-Key": "dashboard:chat-1"})
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "refused"


class TestThePreviewRouteOverHttp:
    async def test_a_staged_template_comes_back_with_its_link(self, vetted, monkeypatch) -> None:
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                json={"template_id": "fixture-board"},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["ok"] is True
        # The PAGE for this crew by name, never the JSON read: the Members page with
        # its Dashboard tab on the staged page. A slug-only or `/api/` link is refused
        # or shows raw JSON.
        assert body["preview"]["preview_url"] == f"/members?member={CREW}&dashboard=preview"
        assert "html" not in body["preview"], "the staged page itself came back on the wire"

    async def test_a_non_string_template_id_is_refused(self, vetted, monkeypatch) -> None:
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                json={"template_id": 7},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "validation_error"

    async def test_a_body_that_is_not_json_is_refused(self, vetted, monkeypatch) -> None:
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                data="not json",
                headers={
                    "X-Session-Key": "dashboard:chat-1",
                    "Content-Type": "application/json",
                },
            )
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "invalid_json"

    async def test_a_json_body_that_is_not_an_object_is_refused(self, vetted, monkeypatch) -> None:
        """A list parses as JSON and carries no fields, so it needs its own answer."""
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                json=["fixture-board"],
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "invalid_body"

    async def test_an_authored_page_is_refused_with_the_stores_own_sentence(
        self, vetted, monkeypatch
    ) -> None:
        """The handler passes `html`/`manifest` through so the STORE can refuse them.

        It must not drop them and stage a template the caller never asked for: a
        caller that sent a page it wrote has to read why its page cannot render.
        """
        store = _page_store(monkeypatch)
        seen: list[dict[str, Any]] = []

        def _stage(_slug, **kw):
            seen.append(kw)
            raise store.InstanceRefused("custom templates come later")

        monkeypatch.setattr(store, "stage_preview", _stage)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                json={"manifest": {"id": "mine"}, "html": "<p></p>"},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 400, await resp.text()
            body = await resp.json()
        assert body["code"] == "refused"
        assert "come later" in body["error"]
        assert seen and seen[0]["html"] == "<p></p>", "the handler swallowed the page"
        assert seen[0]["manifest"] == {"id": "mine"}

    async def test_an_unexpected_store_failure_is_503_and_leaks_nothing(
        self, vetted, monkeypatch
    ) -> None:
        """A refusal is written for an agent; an OSError carries a path.

        So the path goes to the log and the caller gets a fixed sentence.
        """
        store = _page_store(monkeypatch)

        def _boom(_slug, **_kw):
            raise OSError("/home/someone/.kirocrew/members/x/instance.json: no space")

        monkeypatch.setattr(store, "stage_preview", _boom)
        async with _client() as c:
            resp = await c.post(
                PREVIEW_PATH,
                json={"template_id": "fixture-board"},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 503, await resp.text()
            body = await resp.json()
        assert body["code"] == "store_failed"
        assert ".kirocrew" not in body["error"], "the refusal carried a filesystem path"


class TestTheApplyAndRollbackRoutesOverHttp:
    async def test_apply_reports_the_new_version_and_tells_the_open_tab(
        self, vetted, monkeypatch
    ) -> None:
        """The broadcast is what makes the tab refetch, and it carries the slug only."""
        _page_store(monkeypatch)
        frames: list[tuple[str, Any]] = []
        async with _client() as c:
            state = c.server.app["state"]
            monkeypatch.setattr(
                state, "broadcast_ws", lambda kind, data: frames.append((kind, data))
            )
            resp = await c.post(
                APPLY_PATH,
                json={"template_id": "fixture-board"},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["instance_version"] == 5 and body["template_id"] == "fixture-board"
        assert "saved_template_id" not in body, "apply still promises a saved template"
        assert frames == [("dashboard_instance_changed", {"slug": SLUG})], frames

    @pytest.mark.parametrize(
        ("path", "body"),
        [(APPLY_PATH, {"template_id": "fixture-board"}), (ROLLBACK_PATH, {"to_version": 2})],
        ids=["apply", "rollback"],
    )
    async def test_the_reply_hands_the_new_page_block_and_upkeep_pointer(
        self, vetted, monkeypatch, path, body
    ) -> None:
        """The member section is rebuilt at session start and after a compaction, not
        every turn, so the reply that changed the page carries its new pointer."""
        from kiro_crew import dashboard_agentic

        _page_store(monkeypatch)
        monkeypatch.setattr(
            dashboard_agentic, "turn_block", lambda slug: f"[DASHBOARD for {slug}]\nUpkeep: x"
        )
        async with _client() as c:
            resp = await c.post(path, json=body, headers={"X-Session-Key": "dashboard:chat-1"})
            assert resp.status == 200, await resp.text()
            reply = await resp.json()
        assert reply["dashboard"] == f"[DASHBOARD for {SLUG}]\nUpkeep: x"

    async def test_apply_with_no_template_and_no_saved_page_is_a_refusal(
        self, vetted, monkeypatch
    ) -> None:
        _page_store(monkeypatch)
        monkeypatch.setattr(
            routes, "_dashboard_artifact_store", lambda: SimpleNamespace(list=lambda **_kw: [])
        )
        async with _client() as c:
            resp = await c.post(APPLY_PATH, json={}, headers={"X-Session-Key": "dashboard:chat-1"})
            assert resp.status == 400, await resp.text()
            body = await resp.json()
        assert body["code"] == "refused" and "nothing to apply" in body["error"]

    async def test_rollback_reports_the_version_it_landed_on(self, vetted, monkeypatch) -> None:
        """Two different numbers: the one asked for, and the one now current.

        A caller that reported the first would tell a person they are on a version
        nobody is on.
        """
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                ROLLBACK_PATH,
                json={"to_version": 2},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()
        assert body["restored_from"] == 2, "the route lost the version asked for"
        assert body["instance_version"] == 7, "a rollback moves FORWARD"

    @pytest.mark.parametrize(
        "to_version",
        [0, -1, "2", 1.5, True, None],
        ids=["zero", "negative", "string", "float", "bool", "absent"],
    )
    async def test_a_version_that_is_not_a_positive_integer_is_refused(
        self, vetted, monkeypatch, to_version: Any
    ) -> None:
        """``True`` is included on purpose: it is an ``int`` to ``isinstance``.

        Without the explicit bool check a rollback to ``True`` would reach the store
        as version 1 -- a real version, restored because a caller sent a flag.
        """
        _page_store(monkeypatch)
        async with _client() as c:
            resp = await c.post(
                ROLLBACK_PATH,
                json={"to_version": to_version},
                headers={"X-Session-Key": "dashboard:chat-1"},
            )
            assert resp.status == 400, await resp.text()
            assert (await resp.json())["code"] == "validation_error"


class TestTheCatalogListingTheAgentReads:
    """`_templates_for_agent` itself, against a real registry scan.

    The route cases above replace this function, because what they are about is the
    HTTP contract. This class is about the LISTING, so it runs the real thing over a
    fixture built-in directory.
    """

    @staticmethod
    def _fixture_catalog(monkeypatch, tmp_path) -> None:
        from kiro_crew.dashboard_templates import catalog

        root = tmp_path / "builtin"
        for tid, title, description, fold, path in (
            ("fixture-board", "Fixture board", "What this crewmate did.", "usage", "cost_usd"),
            ("fixture-plain", "Fixture plain", "A second page.", "workstreams", "items"),
        ):
            directory = root / tid
            directory.mkdir(parents=True)
            (directory / "manifest.json").write_text(
                json.dumps(
                    {
                        "id": tid,
                        "version": 1,
                        "title": title,
                        "description": description,
                        "source": "builtin",
                        "fields": {
                            "shown": {"type": "number", "source": {"fold": fold, "path": path}}
                        },
                    }
                ),
                encoding="utf-8",
            )
            (directory / "template.html").write_text(
                '<p data-dashboard-field="shown"></p>', encoding="utf-8"
            )
        monkeypatch.setattr(catalog, "builtin_dir", lambda: root)

    def test_it_marks_the_current_template_rather_than_hiding_it(
        self, monkeypatch, tmp_path
    ) -> None:
        """A list that omitted the page on screen would read as a missing template."""
        self._fixture_catalog(monkeypatch, tmp_path)
        _page_store(monkeypatch)
        body = routes._templates_for_agent(SLUG, "")
        rows = {row["id"]: row for row in body["templates"]}
        assert set(rows) == {"fixture-board", "fixture-plain"}
        assert rows["fixture-board"]["current"] is True
        assert rows["fixture-plain"]["current"] is False
        assert body["current_template_id"] == "fixture-board"

    def test_a_query_matches_a_word_only_a_fold_path_carries(self, monkeypatch, tmp_path) -> None:
        """The reason the listing carries `paths` at all.

        "the one that shows cost" names what the reader wants to SEE, and the template
        that shows it is the one whose field reads a cost out of a usage fold -- a word
        its author never put in the title or the description.
        """
        self._fixture_catalog(monkeypatch, tmp_path)
        _page_store(monkeypatch)
        body = routes._templates_for_agent(SLUG, "cost")
        assert [row["id"] for row in body["templates"]] == ["fixture-board"]
        assert body["query"] == "cost"

    def test_a_query_that_matches_nothing_answers_an_empty_list(
        self, monkeypatch, tmp_path
    ) -> None:
        self._fixture_catalog(monkeypatch, tmp_path)
        _page_store(monkeypatch)
        assert routes._templates_for_agent(SLUG, "latency")["templates"] == []

    def test_a_directory_that_will_not_load_is_reported_not_dropped(
        self, monkeypatch, tmp_path
    ) -> None:
        """A template nobody can see is a template nobody can fix.

        The scan reports it, so the agent reading this listing can tell the human.
        """
        self._fixture_catalog(monkeypatch, tmp_path)
        broken = tmp_path / "builtin" / "fixture-broken"
        broken.mkdir()
        (broken / "manifest.json").write_text("{ not json", encoding="utf-8")
        (broken / "template.html").write_text("<p></p>", encoding="utf-8")
        _page_store(monkeypatch)
        body = routes._templates_for_agent(SLUG, "")
        assert [p["template"] for p in body["problems"]] == ["fixture-broken"]
        assert {row["id"] for row in body["templates"]} == {"fixture-board", "fixture-plain"}

    def test_an_unreadable_current_template_leaves_the_listing_answerable(
        self, monkeypatch, tmp_path
    ) -> None:
        """A damaged record must not take the catalog down with it.

        The list is how a reader gets OFF a broken page, so a record this cannot read
        marks nothing as current rather than refusing the only route out.
        """
        self._fixture_catalog(monkeypatch, tmp_path)

        def _unreadable(_slug):
            raise OSError("instance.json is a directory")

        _page_store(monkeypatch, read=_unreadable)
        body = routes._templates_for_agent(SLUG, "")
        assert body["current_template_id"] == ""
        assert len(body["templates"]) == 2
        assert all(row["current"] is False for row in body["templates"])


class TestTheHistorySessionLookup:
    """`_history_session`: which DM unit a change is attributed to."""

    def test_no_slot_is_no_session_and_not_a_failure(self) -> None:
        assert routes._history_session("") == ""

    def test_the_newest_unit_of_the_slot_is_the_one_chosen(self, monkeypatch) -> None:
        """The NEWEST, because a change is being recorded now.

        A clock that stepped backward between two units would otherwise attribute a
        later change to a session that is already over.
        """
        monkeypatch.setattr(
            crew_log_projection, "units_in_succession", lambda _slot: ["older", "newest"]
        )
        assert routes._history_session("dashboard:chat-1") == "newest"

    def test_a_slot_with_no_unit_yet_answers_empty(self, monkeypatch) -> None:
        monkeypatch.setattr(crew_log_projection, "units_in_succession", lambda _slot: [])
        assert routes._history_session("dashboard:chat-1") == ""

    def test_a_projection_failure_degrades_to_no_history_rather_than_refusing(
        self, monkeypatch
    ) -> None:
        """The record is the file; the entry is history.

        So a crewmate whose DM thread cannot be read gets a working dashboard with no
        history row, not a refused change.
        """

        def _boom(_slot):
            raise RuntimeError("the projection kernel is not up")

        monkeypatch.setattr(crew_log_projection, "units_in_succession", _boom)
        assert routes._history_session("dashboard:chat-1") == ""
