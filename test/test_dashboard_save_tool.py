"""Tests for ``dashboard_save`` -- the call that puts a composed page IN FORCE.

There is no preview gate: the owner crewmate's package is stored and
drawn at once, as an instance version, and one ``dashboard_rollback`` goes back to
any earlier version -- another package version or the template page.

The binding is derived from the vetted caller on the server and never read out of
the body, so a save only ever writes the caller's own package.

Nothing here reimplements the store's own write gate. The save runs
``canonical_package_content`` -- the same function ``ArtifactStore.create`` and
``.update`` run -- and the refusal tests send packages each guard refuses and
assert the save answers 400 with nothing stored.
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import platform_compat
from kiro_crew.artifact_store.dashboard_package import DASHBOARD_KIND, parse_package
from kiro_crew.dashboard.handlers import agent_panel as routes
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.asyncio

CREW = "fleet-crew"
SLUG = "fleet-crew"
OTHER_SLUG = "other-crew"


#: A crewmate's page is view.html only; two orders of the same two cells, so a save
#: that swaps them is a layout change.
_PAGE_LEAD_FIRST = (
    '<p data-dashboard-field="open_prs"></p>\n<p data-dashboard-field="headline"></p>'
)
_PAGE_NOTE_FIRST = (
    '<p data-dashboard-field="headline"></p>\n<p data-dashboard-field="open_prs"></p>'
)


def _flip(package: dict[str, Any]) -> None:
    """Swap the page's two cells: the html counterpart of reordering the layout."""
    view = package["view"]
    view["html"] = _PAGE_NOTE_FIRST if view["html"] == _PAGE_LEAD_FIRST else _PAGE_LEAD_FIRST


def _package_body() -> dict[str, Any]:
    """The three declarations a save takes. No ``kind``, and no ``bound_to``."""
    return {
        "model": {
            "types": {
                "open_prs": {
                    "type": "number",
                    "source": {"agentic": True},
                    "label": "Open PRs",
                },
                "headline": {"type": "text", "source": {"agentic": True}},
            }
        },
        "view": {"html": _PAGE_LEAD_FIRST},
        "theme": {"tokens": {"--accent": "oklch(0.7 0.15 150)"}},
    }


class _Store:
    """A stand-in for ``ArtifactStore`` that keeps the real write gate.

    The gate is the point of the test, so this does NOT fake it: ``create`` and
    ``update`` call ``parse_package`` exactly where the real store calls
    ``canonical_package_content``, and raise the same
    ``ArtifactValidationError``. What is faked is only the filesystem -- the
    records live in a dict -- so a route test needs no artifact home.
    """

    def __init__(self) -> None:
        self.records: dict[str, SimpleNamespace] = {}
        self.updates: list[dict[str, Any]] = []
        #: Slugs whose ``get`` fails. A record that cannot be OPENED is the case
        #: the binding lookup must not answer "absent" for, because it may be the
        #: one bound here -- that is exactly what could not be read.
        self.unreadable: set[str] = set()

    # -- the surface the handler uses -------------------------------------- #

    def list(self, *, kind: str | None = None, **_kw: Any) -> list[SimpleNamespace]:
        return [r for r in self.records.values() if kind is None or r.kind == kind]

    def get(self, slug: str, **_kw: Any) -> SimpleNamespace:
        from kiro_crew.artifact_store.model import ArtifactNotFoundError

        if slug in self.unreadable:
            from kiro_crew.artifact_store.model import ArtifactError

            raise ArtifactError(f"{slug} cannot be opened")
        if slug not in self.records:
            raise ArtifactNotFoundError(slug)
        return self.records[slug]

    def create(self, *, name: str, content: str, kind: str | None = None, **_kw: Any):
        from kiro_crew.artifact_store.dashboard_package import canonical_package_content

        if kind == DASHBOARD_KIND:
            content = canonical_package_content(content)
        slug = f"dashboard-{len(self.records)}"
        rec = SimpleNamespace(
            slug=slug,
            name=name,
            content=content,
            kind=kind or "markdown",
            version=1,
            content_token="0" * 64,
        )
        self.records[slug] = rec
        return rec

    def update(self, slug: str, *, content: str | None = None, **kw: Any):
        from kiro_crew.artifact_store.dashboard_package import (
            canonical_package_content,
            layout_changed,
        )

        rec = self.get(slug)
        self.updates.append({"slug": slug, **kw})
        if content is not None:
            new = canonical_package_content(content)
            if layout_changed(rec.content, new):
                rec.version += 1
            rec.content = new
        return rec


@pytest.fixture
def store(monkeypatch) -> _Store:
    s = _Store()
    monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: s, raising=False)
    return s


@asynccontextmanager
async def _client(monkeypatch, *, resolved: tuple[str, str, str] | None = (SLUG, CREW, "slot-1")):
    """The panel routes with the caller already vetted, or already refused.

    ``resolved=None`` is the shape every caller the shared gate turns away has --
    a subagent with no dashboard slot, a cookie-only caller, an app identity, a
    restricted session. Those refusals are ``_resolve_dashboard_caller``'s and are
    proven on its own tests; what this file has to prove is that a save ASKS it
    and writes nothing when it says no.
    """

    async def _resolve(_request, _operation):
        if resolved is None:
            return None, web.json_response(
                {"error": "not a dashboard thread", "code": "no_dashboard_slot"}, status=400
            )
        return resolved, None

    monkeypatch.setattr(routes, "_resolve_dashboard_caller", _resolve)
    app = web.Application()
    app["state"] = SimpleNamespace(broadcast_ws=lambda *_a, **_k: None)
    routes.register_agent_panel_routes(app)
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


async def _save(client: TestClient, body: dict[str, Any]):
    return await client.post("/api/agent-panel/dashboard/save", json=body)


async def _save_and_apply(client: TestClient, body: dict[str, Any]) -> None:
    """Save a page, then put it live: the package flow is save -> apply."""
    saved = await _save(client, body)
    assert saved.status == 200, await saved.text()
    applied = await _apply(client)
    assert applied.status == 200, await applied.text()


async def _apply(client: TestClient):
    """The person's "yes". Takes no body, like the route."""
    return await client.post("/api/agent-panel/dashboard/apply", json={})


# --------------------------------------------------------------------------- #
# 0. A save puts the page in force, as a version
# --------------------------------------------------------------------------- #


def _instance():
    from kiro_crew.dashboard_templates import instance as instance_store

    return instance_store


async def test_a_save_stores_the_page_and_apply_puts_it_live(monkeypatch, store):
    """Save stores the caller's own package; apply puts it live, with no preview step.

    The package is bound to the CALLER, and the instance version the apply writes
    pins it, so the page read and the write check both draw it.
    """
    async with _client(monkeypatch) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 200, await resp.text()
        payload = await resp.json()
        # Stored, not live yet.
        assert _instance().read(SLUG).instance_version == 0
        applied = await _apply(client)
        assert applied.status == 200, await applied.text()
        reply = await applied.json()

    saved = payload["saved"]
    assert (saved["fields"], saved["blocks"]) == (2, 0)
    [record] = store.records.values()
    assert record.kind == DASHBOARD_KIND
    assert parse_package(record.content)["bound_to"] == f"crewmate:{SLUG}"
    assert saved["package"] == reply["package"] == record.slug
    current = _instance().read(SLUG)
    assert (current.package_slug, current.package_version) == (record.slug, 1)
    assert current.instance_version == reply["instance_version"] == 1


async def test_a_second_save_updates_the_same_package_under_its_read(monkeypatch, store):
    """One package per crewmate: a second save updates it, fenced on the read."""
    second = _package_body()
    _flip(second)
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        await _save_and_apply(client, second)

    assert len(store.records) == 1
    [update] = store.updates
    assert "snapshot_token" in update
    current = _instance().read(SLUG)
    assert (current.package_version, current.instance_version) == (2, 2)


async def test_one_rollback_goes_back_to_a_package_or_to_the_template(monkeypatch, store):
    """Every apply is a version, and the version restores its own page in force."""
    instance_store = _instance()
    second = _package_body()
    _flip(second)
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())  # v1: package v1
        await _save_and_apply(client, second)  # v2: package v2
        applied = await client.post(
            "/api/agent-panel/dashboard/apply", json={"template_id": "project-report"}
        )
        assert applied.status == 200, await applied.text()  # v3: template
        assert instance_store.read(SLUG).package_released is True
        back = await client.post("/api/agent-panel/dashboard/rollback", json={"to_version": 1})
        assert back.status == 200, await back.text()  # v4: package v1 again

    current = instance_store.read(SLUG)
    assert (current.instance_version, current.package_version) == (4, 1)
    assert current.package_released is False


async def test_the_page_read_draws_what_the_record_puts_in_force(monkeypatch, store):
    """The page read follows the record: pinned, locked, then released."""
    from kiro_crew.dashboard.handlers import member_dashboard

    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        [slug] = store.records
        pinned, older, locked = member_dashboard._package_in_force(SLUG)
        assert pinned is not None and (pinned.slug, pinned.version) == (slug, 1)
        assert older is False and locked is False
        # The lock keeps the pinned page the person locked, and the page reports it.
        _instance().set_structure_lock(SLUG, True)
        assert member_dashboard._package_in_force(SLUG)[::2] == (pinned, True)
        _instance().set_structure_lock(SLUG, False)
        applied = await client.post(
            "/api/agent-panel/dashboard/apply", json={"template_id": "project-report"}
        )
        assert applied.status == 200, await applied.text()
        assert member_dashboard._package_in_force(SLUG) == (None, False, False)


async def test_the_pinned_page_survives_whatever_happens_to_the_artifact(monkeypatch, store):
    """The page in force is the gateway's snapshot of the saved bytes.

    An artifact deleted and saved again under the same slug, or a pinned artifact
    version the store has pruned, does not change or drop the page a version pins.
    """
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
    [slug] = store.records
    saved = store.records[slug].content
    # Deleted, and a different page saved under the same slug and version.
    other = _package_body()
    _flip(other)
    other.update({"kind": DASHBOARD_KIND, "bound_to": f"crewmate:{SLUG}"})
    store.records.clear()
    store.create(name="again", content=json.dumps(other), kind=DASHBOARD_KIND)
    pinned = _instance().pinned_package(_instance().read(SLUG))
    assert pinned is not None and pinned.content == saved


async def test_only_pinned_pages_are_kept_after_a_save(monkeypatch, store):
    """A save prunes the snapshots no kept version pins, and keeps every pinned one."""
    from kiro_crew.agent_panel import panel_dir

    second = _package_body()
    _flip(second)
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        await _save_and_apply(client, second)
    pages = sorted(p.name for p in panel_dir().glob(f"{SLUG}.dashboard-page.*.json"))
    # Both versions are kept, so both pages stay reachable by a rollback.
    assert len(pages) == 2, pages
    # A page nobody pins is dropped on the next save.
    stray = panel_dir() / f"{SLUG}.dashboard-page.{'0' * 32}.json"
    stray.write_text("{}", encoding="utf-8")
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
    assert not stray.exists()


async def test_an_older_locked_record_checks_writes_against_its_template(monkeypatch, tmp_path):
    """A record from before pins keeps its template under the lock, for the write too."""
    from kiro_crew import artifacts
    from kiro_crew import dashboard_package as reader
    from kiro_crew.artifacts import ArtifactStore

    instance_store = _instance()
    real = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(artifacts, "get_default_store", lambda: real)
    instance_store.adopt(SLUG, "project-report")
    # Written before the `package` key existed.
    path = instance_store.instance_dir(SLUG) / "instance.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("package")
    path.write_text(json.dumps(raw), encoding="utf-8")
    # A package bound to the crewmate, with a version a person cut.
    pkg = {**_package_body(), "kind": DASHBOARD_KIND, "bound_to": f"crewmate:{SLUG}"}
    art = real.create(name="page", content=json.dumps(pkg), kind=DASHBOARD_KIND)
    _flip(pkg)
    read = real.get(art.slug, assign_token=True)
    real.update(art.slug, content=json.dumps(pkg), actor="user", snapshot_token=read.content_token)

    assert reader.read_package_model(SLUG).from_package is True
    instance_store.set_structure_lock(SLUG, True)
    locked = reader.read_package_model(SLUG)
    assert locked.from_package is False
    assert locked.model is not None and locked.model.manifest.id == "project-report"


def test_a_package_enum_keeps_its_choices_for_the_write_check():
    """A value outside an `enum` field's declared choices is refused before it lands."""
    from kiro_crew import dashboard_agentic
    from kiro_crew.dashboard.handlers.member_dashboard_fields import package_manifest

    package = {
        "model": {
            "types": {
                "state": {
                    "type": "enum",
                    "choices": ["green", "red"],
                    "source": {"agentic": True},
                }
            }
        }
    }
    model = dashboard_agentic.Instance(
        manifest=package_manifest(package, slug="p", version=1), instance_version=1
    )
    dashboard_agentic.check_write(model, "state", "green", {})
    with pytest.raises(dashboard_agentic.WriteRefused):
        dashboard_agentic.check_write(model, "state", "purple", {})


async def test_a_package_rebound_before_the_fenced_read_is_not_written(monkeypatch, store):
    """The save re-checks the binding on the read whose token fences the update."""
    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        [slug] = store.records
        # Rebound to another crewmate between the lookup and the fenced read.
        theirs = {**_package_body(), "kind": DASHBOARD_KIND, "bound_to": f"crewmate:{OTHER_SLUG}"}
        from kiro_crew.artifact_store.dashboard_package import canonical_package_content

        real_get = store.get

        def _rebound_get(s: str, **kw: Any) -> SimpleNamespace:
            rec = real_get(s, **kw)
            if kw.get("assign_token"):
                rec.content = canonical_package_content(json.dumps(theirs))
            return rec

        monkeypatch.setattr(store, "get", _rebound_get)
        resp = await _save(client, _package_body())
        body = await resp.json()
    assert resp.status == 400, body
    assert "not bound to you" in body["error"]
    assert store.updates == []
    assert slug in store.records


async def test_any_apply_drops_a_preview_left_staged(monkeypatch, store):
    """A committed version replaces a staged look, so the tab never offers a stale Keep."""
    instance_store = _instance()
    async with _client(monkeypatch) as client:
        instance_store.stage_preview(SLUG, template_id="project-report")
        await _save_and_apply(client, _package_body())
        assert instance_store.staged_preview(SLUG) is None
        instance_store.stage_preview(SLUG, template_id="project-report")
        applied = await client.post(
            "/api/agent-panel/dashboard/apply", json={"template_id": "project-report"}
        )
        assert applied.status == 200, await applied.text()
        assert instance_store.staged_preview(SLUG) is None


async def test_a_scan_that_cannot_read_a_kept_version_prunes_nothing(monkeypatch, store):
    """A kept version that will not read may pin a page, so nothing is deleted."""
    from kiro_crew.agent_panel import panel_dir

    instance_store = _instance()
    second = _package_body()
    _flip(second)
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())
        stray = panel_dir() / f"{SLUG}.dashboard-page.{'0' * 32}.json"
        stray.write_text("{}", encoding="utf-8")

        def _unreadable(_slug: str, _version: int) -> dict:
            raise instance_store.InstanceError("instance version cannot be read")

        monkeypatch.setattr(instance_store, "_read_version", _unreadable)
        await _save_and_apply(client, second)
    assert stray.exists()
    assert len(list(panel_dir().glob(f"{SLUG}.dashboard-page.*.json"))) == 3


def test_a_concurrent_prune_never_drops_the_page_being_committed(monkeypatch):
    """The snapshot write, the commit and the prune share one lock."""
    instance_store = _instance()
    seen: list[bool] = []
    real_prune = instance_store._prune_page_snapshots

    def _prune(slug: str) -> None:
        # Asked from inside the commit's critical section: the record already pins it.
        record = instance_store._read_record(slug) or {}
        seen.append(bool(instance_store._package_field(record)[2]))
        real_prune(slug)

    monkeypatch.setattr(instance_store, "_prune_page_snapshots", _prune)
    pkg = {**_package_body(), "kind": DASHBOARD_KIND, "bound_to": f"crewmate:{SLUG}"}
    from kiro_crew.artifact_store.dashboard_package import canonical_package_content

    record = instance_store.apply_package(
        SLUG,
        package_slug="p",
        package_version=1,
        content=canonical_package_content(json.dumps(pkg)),
    )
    assert seen == [True]
    assert instance_store.pinned_package(record) is not None


async def test_a_template_applies_without_a_preview(monkeypatch, store):
    """A preview is an optional look: ``template_id`` applies directly."""
    async with _client(monkeypatch) as client:
        resp = await client.post(
            "/api/agent-panel/dashboard/apply", json={"template_id": "project-report"}
        )
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["template_id"] == "project-report"
        unknown = await client.post(
            "/api/agent-panel/dashboard/apply", json={"template_id": "no-such-page"}
        )
        assert unknown.status == 400, await unknown.text()
    assert _instance().read(SLUG).instance_version == 1


async def test_a_root_session_save_is_refused_and_writes_nothing(monkeypatch, store):
    """The hosted-page reader resolves a crewmate's binding only, so nothing would draw."""
    session_slug = _instance().session_instance_key("chat-7-17910")
    async with _client(monkeypatch, resolved=(session_slug, "", "chat-7-17910")) as client:
        resp = await _save(client, _package_body())
        body = await resp.json()
    assert resp.status == 400, body
    assert body["code"] == "session_package_unsupported"
    assert store.records == {}


async def test_a_locked_layout_refuses_the_apply_and_keeps_the_page(monkeypatch, store):
    """The lock refuses the apply; the save is only a write to the crewmate's artifact."""
    instance_store = _instance()
    instance_store.set_structure_lock(SLUG, True)
    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        resp = await _apply(client)
        body = await resp.json()
    assert resp.status == 423, body
    assert body["code"] == "dashboard_locked"
    assert instance_store.read(SLUG).package_slug == ""


async def test_dashboard_write_is_checked_against_the_package_in_force(monkeypatch, tmp_path):
    """The write guard is lifted: a bound package's own Model checks the write."""
    from kiro_crew import artifacts, dashboard_agentic
    from kiro_crew import dashboard_package as reader
    from kiro_crew.artifacts import ArtifactStore

    real = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: real)
    monkeypatch.setattr(artifacts, "get_default_store", lambda: real)
    async with _client(monkeypatch) as client:
        await _save_and_apply(client, _package_body())

    model = reader.read_package_model(SLUG)
    assert model.from_package is True and model.model is not None
    assert set(model.model.manifest.fields) == {"open_prs", "headline"}
    dashboard_agentic.check_write(model.model, "open_prs", 3, {})
    with pytest.raises(dashboard_agentic.WriteRefused):
        dashboard_agentic.check_write(model.model, "open_prs", "three", {})


async def test_an_apply_with_nothing_staged_writes_nothing(monkeypatch, store):
    """No template named and nothing staged: the apply has nothing to put in force."""
    async with _client(monkeypatch) as client:
        resp = await _apply(client)
        assert resp.status == 400, await resp.text()

    assert store.records == {}
    assert store.updates == []


# --------------------------------------------------------------------------- #
# 1. An applied page round-trips
# --------------------------------------------------------------------------- #


async def test_the_binding_comes_from_the_caller_and_the_body_cannot_carry_one(monkeypatch, store):
    """``bound_to`` in the body is refused, not merged and not ignored.

    Ignoring it would be safe and still wrong: an agent that sent one would be
    told its save succeeded while the page went somewhere else, and would send it
    again every cycle. The refusal is the only answer it can act on.
    """
    body = _package_body()
    body["bound_to"] = f"crewmate:{OTHER_SLUG}"
    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400
        payload = await resp.json()
    assert "bound_to" in payload["error"]
    assert store.records == {}


# --------------------------------------------------------------------------- #
# 2. An unchanged layout cuts no version
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# 3. An invalid package is refused with a 400-class error
# --------------------------------------------------------------------------- #


async def test_a_block_layout_is_refused_for_a_crewmate_page(monkeypatch, store):
    """A crewmate's page is drawn from view.html only, so a view of blocks is refused
    at the save, before anything is stored, with the field-binding rule to follow."""
    body = _package_body()
    body["view"] = {"blocks": [{"id": "lead", "type": "stat", "fields": ["open_prs"]}]}
    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400
        payload = await resp.json()
    assert payload["code"] == "invalid_package"
    assert "view.html only" in payload["error"]
    assert "data-dashboard-field" in payload["error"]
    assert store.records == {}


async def test_an_oversized_package_is_refused(monkeypatch, store):
    """Over the package cap, refused as a package and not as a 500."""
    body = _package_body()
    body["model"]["types"]["open_prs"]["description"] = "x" * 400_000
    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400
        payload = await resp.json()
    assert payload["code"] == "invalid_package"
    assert store.records == {}


def _lone_surrogate_body() -> bytes:
    """A package whose page text holds an unpaired ``\\ud800``, as raw JSON bytes.

    Raw rather than through ``json=``: the escape has to reach the handler AS
    JSON, because it is six ASCII characters until the decoder turns it into the
    single code point no UTF-8 encoder accepts.
    """
    body = _package_body()
    body["view"]["html"] += "<p>LONE</p>"
    return json.dumps(body).replace("LONE", "\\ud800").encode("utf-8")


async def test_a_lone_surrogate_is_refused(monkeypatch, store):
    async with _client(monkeypatch) as client:
        resp = await client.post(
            "/api/agent-panel/dashboard/save",
            data=_lone_surrogate_body(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400
    assert store.records == {}


async def test_a_lone_surrogate_never_reaches_the_real_store_either(monkeypatch, tmp_path):
    """The same body with a REAL store behind the route, which it must never reach.

    The refusal is the save's now, because the save runs the dashboard gate itself
    and nothing is handed to a store until an apply. So the thing worth pinning
    with a real store wired up is that the store stays EMPTY: no record, and no
    apply to make one, because the package was refused while the agent was still
    composing. The store's own refusal, for every other caller, is pinned in
    ``test_artifact_update_refuses_a_surrogate_the_same_way``.
    """
    from kiro_crew.artifacts import ArtifactStore

    real = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: real, raising=False)
    async with _client(monkeypatch) as client:
        resp = await client.post(
            "/api/agent-panel/dashboard/save",
            data=_lone_surrogate_body(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400, await resp.text()
        assert (await resp.json())["code"] == "invalid_package"
        # And nothing was staged for an apply to pick up.
        assert (await _apply(client)).status == 400
    assert real.list(kind=DASHBOARD_KIND) == []


async def test_artifact_update_refuses_a_surrogate_the_same_way(tmp_path):
    """The store's OWN refusal, on ``update`` as well as ``create``.

    The clean refusal belongs to the store, so it holds for every caller --
    including a plain ``artifact_update`` the save route never touches.
    ``_validate_content`` is the first call in both methods and it measures
    content by encoding it, and content holding an unpaired surrogate cannot be
    encoded at all: answering that with ``UnicodeEncodeError`` would turn an
    artifact a caller can fix into a 500.
    """
    from kiro_crew.artifact_store.model import ArtifactValidationError
    from kiro_crew.artifacts import ArtifactStore

    store = ArtifactStore(tmp_path / "artifacts")
    pkg = _package_body()
    pkg["kind"] = DASHBOARD_KIND
    pkg["bound_to"] = f"crewmate:{SLUG}"
    created = store.create(
        name="page", content=json.dumps(pkg), kind=DASHBOARD_KIND, source="dashboard"
    )

    # A REAL lone code point in the content text, which is what reaches the store:
    # the escape ``\\ud800`` is six ASCII characters and encodes fine, so a package
    # built by replacing text with it would exercise the decoder's refusal instead
    # and pass whatever ``_validate_content`` does. ``ensure_ascii=False`` is what
    # keeps the code point itself in the output, and is what the save route uses.
    surrogate = dict(pkg)
    surrogate["view"] = {"html": pkg["view"]["html"] + "<p>\ud800</p>"}
    bad = json.dumps(surrogate, ensure_ascii=False)
    assert "\ud800" in bad
    # UNDER THE RECORD'S OWN TOKEN, the way an owner write reaches a dashboard
    # record: `update` refuses a content write on `kind="dashboard"` that does not
    # name the read it was authorized against, and an absent token is its own
    # refusal (`ArtifactConflictError`). Passing it keeps this test pointed at the
    # surrogate refusal rather than at the owner gate, which has its own tests.
    token = store.get(created.slug, assign_token=True).content_token
    with pytest.raises(ArtifactValidationError) as update_refusal:
        store.update(created.slug, content=bad, snapshot_token=token)
    assert "surrogate" in str(update_refusal.value)

    with pytest.raises(ArtifactValidationError) as create_refusal:
        store.create(name="other", content=bad, kind=DASHBOARD_KIND, source="dashboard")
    assert "surrogate" in str(create_refusal.value)

    # The live package is untouched by the refused update, and the control: the
    # same bytes without the surrogate are still accepted, so the refusal above
    # is the surrogate's and not the whole package being rejected.
    assert parse_package(store.get(created.slug).content)["bound_to"] == f"crewmate:{SLUG}"
    live = store.get(created.slug, assign_token=True)
    assert store.update(created.slug, content=json.dumps(pkg), snapshot_token=live.content_token)


async def test_a_credential_shaped_field_name_is_refused_at_save(monkeypatch, store):
    """A name a redactor would mask is refused HERE, and nothing is written.

    The field-name grammar both sides enforce, ``[a-z][a-z0-9_]{0,63}``, admits a
    credential shape: ``ghp_`` plus 36 lowercase characters satisfies it. The page
    refuses a read whose declared names a redactor would rewrite, and it refuses
    the WHOLE read, because a field name is the key joining the read's fields, the
    frame's blocks and the page's cells. So a save that stored such a name would
    succeed and hand the crewmate a page that can never render -- while this
    route's reply says the tab draws it. That reply would be false, so the save
    refuses instead.

    The premise is asserted on the REDACTOR rather than on a hand-written pattern,
    because the redactor is what both sides consult. A test that invented its own
    pattern would pin a rule nothing enforces.

    The refusal names a POSITION, not the name. The page's own counter withholds
    the offending name on purpose, and the MCP boundary redacts this sentence
    anyway, so a name would reach the agent masked while travelling raw through the
    gateway's logs. A position resolves against the package in the agent's own hand.
    """
    from kiro_crew.platform import redact_via_context

    token_name = "ghp_" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8"
    # The two halves of the premise, each asserted rather than assumed.
    assert redact_via_context(token_name) != token_name, "not credential-shaped here"
    assert redact_via_context("open_prs") == "open_prs", "control name is not redacted"

    body = _package_body()
    body["model"]["types"][token_name] = {"type": "number", "source": {"agentic": True}}
    body["view"]["html"] += f'<p data-dashboard-field="{token_name}"></p>'

    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400, await resp.text()
        payload = await resp.json()

    assert payload["code"] == "unsafe_declared_name"
    assert "looks like a credential" in payload["error"]
    # A position the agent can resolve, and NOT the name itself.
    assert "declared field #" in payload["error"]
    assert token_name not in payload["error"], "the refusal echoed the name back"
    # NOTHING WRITTEN. A refusal that minted the artifact first would be a hole the
    # status code hides, which is the assertion every other refusal here makes.
    assert store.records == {}
    assert store.updates == []


async def test_an_ordinary_field_name_still_saves(monkeypatch, store):
    """The control for the refusal above, and why it is not a blanket block.

    Without it, a route that refused EVERY package would pass that test. The body
    is this file's ordinary one, whose names no redactor touches.
    """
    async with _client(monkeypatch) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 200, await resp.text()

    assert len(store.records) == 1


async def test_a_credential_shaped_theme_token_is_refused_too(monkeypatch, store):
    """The THIRD name set a package carries, and the one whose grammar hides it.

    A field name and a block id match ``[a-z][a-z0-9_]{0,63}``. A theme token is a
    CSS custom property, ``--[a-z][a-z0-9-]{0,63}``, so a lowercase HYPHENATED
    look-alike satisfies it where an underscored one never could -- which is how a
    check written for the first two name sets leaves this one open.

    Refused rather than redacted on the way out, for the reason the other two are:
    a token key is a JOIN KEY. The page's own style references the variable by
    name, so a redactor rewriting it hands back a stylesheet pointing at a property
    that does not exist. There is no masked form that still works.
    """
    from kiro_crew.platform import redact_via_context

    token_key = "--xoxb-" + "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8"
    # The two halves of the premise, each asserted rather than assumed.
    assert redact_via_context(token_key) != token_key, "not credential-shaped here"
    assert redact_via_context("--accent") == "--accent", "control token is not redacted"

    body = _package_body()
    body["theme"]["tokens"][token_key] = "oklch(0.5 0.1 200)"

    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400, await resp.text()
        payload = await resp.json()

    assert payload["code"] == "unsafe_declared_name"
    # A position the agent can resolve, and NOT the name itself.
    assert "theme token #" in payload["error"]
    assert token_key not in payload["error"], "the refusal echoed the name back"
    # NOTHING STAGED and nothing stored, so no apply can reach it either.
    assert store.records == {}
    async with _client(monkeypatch) as client:
        assert (await _apply(client)).status == 400


async def test_an_ordinary_theme_token_still_saves(monkeypatch, store):
    """The control: without it, a route refusing EVERY theme would pass above."""
    body = _package_body()
    body["theme"]["tokens"]["--surface"] = "oklch(0.98 0.01 250)"
    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 200, await resp.text()

    assert len(store.records) == 1


async def test_the_real_store_round_trips_a_valid_package(monkeypatch, tmp_path):
    """The positive control for the surrogate test, through the real store.

    Without it, a route that refused EVERY package would pass the surrogate test,
    so this proves the 400 there came from the surrogate and not from the save path
    being broken against a real store.
    """
    from kiro_crew.artifacts import ArtifactStore

    real = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: real)
    async with _client(monkeypatch) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 200, await resp.text()
        assert "saved" in await resp.json()

    [stored] = real.list(kind=DASHBOARD_KIND)
    assert parse_package(real.get(stored.slug).content)["bound_to"] == f"crewmate:{SLUG}"


# --------------------------------------------------------------------------- #
# 4. A caller the shared gate turns away saves nothing
# --------------------------------------------------------------------------- #


async def test_a_caller_with_no_dashboard_slot_is_refused(monkeypatch, store):
    """The subagent case. A subagent holds no slot, so the shared gate refuses it.

    Asserting the STORE stayed empty is the part that matters: a save that
    answered 400 after minting the artifact would be a hole the status code hides.
    """
    async with _client(monkeypatch, resolved=None) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 400
        assert (await resp.json())["code"] == "no_dashboard_slot"
    assert store.records == {}


# --------------------------------------------------------------------------- #
# 5. A caller cannot save into another crewmate's panel
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# The MCP tool half
# --------------------------------------------------------------------------- #


def test_the_tool_is_on_the_panel_server_and_takes_no_target():
    from kiro_crew import mcp_panel

    tool = next(t for t in mcp_panel._tool_definitions() if t["name"] == "dashboard_save")
    props = tool["inputSchema"]["properties"]
    assert set(props) == {"model", "view", "theme"}
    assert sorted(tool["inputSchema"]["required"]) == ["model", "theme", "view"]
    assert not (set(props) & {"bound_to", "session", "session_key", "slot", "target", "kind"})


def test_the_tool_refuses_a_caller_whose_identity_is_not_strict(monkeypatch):
    """A subagent, answered by the tool before any request leaves the process."""
    from kiro_crew import mcp_panel

    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("", "Error: no identity"))

    def _boom(*_a: Any, **_k: Any):  # pragma: no cover - must not run
        raise AssertionError("a caller with no strict identity reached the gateway")

    monkeypatch.setattr(mcp_panel, "_post", _boom)
    out = mcp_panel._call_tool_inner("dashboard_save", _package_body())
    assert out == "Error: no identity"


def test_the_tool_sends_only_the_three_declarations(monkeypatch):
    from kiro_crew import mcp_panel

    sent: dict[str, Any] = {}
    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("sk", ""))

    def _post(path: str, payload: dict[str, Any], **_kw: Any) -> dict[str, Any]:
        sent["path"] = path
        sent["payload"] = payload
        return {
            "saved": {
                "package": "p",
                "package_version": 1,
                "instance_version": 3,
                "fields": 2,
                "blocks": 2,
            },
            "dashboard": "[DASHBOARD]\nYour Dashboard tab shows the page you saved.",
        }

    monkeypatch.setattr(mcp_panel, "_post", _post)
    body = _package_body()
    body["bound_to"] = "crewmate:someone-else"
    out = mcp_panel._call_tool_inner("dashboard_save", body)
    assert sent["path"] == "/api/agent-panel/dashboard/save"
    assert set(sent["payload"]) == {"model", "view", "theme"}
    # SAVED, not live: the reply names the apply that puts it live.
    assert "SAVED" in out and "not live yet" in out
    assert "dashboard_apply" in out


def test_the_tool_hands_back_a_refusal_whole(monkeypatch):
    """The refusal names the key and the catalog, which is what the agent fixes from."""
    from kiro_crew import mcp_panel

    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("sk", ""))
    monkeypatch.setattr(
        mcp_panel,
        "_post",
        lambda *_a, **_k: {
            "error": "dashboard package: view.blocks[0].type: unknown block type 'hologram'",
            "code": "invalid_package",
        },
    )
    out = mcp_panel._call_tool_inner("dashboard_save", _package_body())
    assert "hologram" in out
    assert "view.blocks[0].type" in out


# --------------------------------------------------------------------------- #
# 6. The grant equals the registration
# --------------------------------------------------------------------------- #


def test_the_member_grant_names_the_save_tool():
    from kiro_crew.agent import _MEMBER_PANEL_GRANTS

    assert "@kirocrew-panel/dashboard_save" in _MEMBER_PANEL_GRANTS


def test_the_tool_has_a_registered_argument_schema():
    from kiro_crew.validation import MCP_PANEL_SCHEMAS

    assert "dashboard_save" in MCP_PANEL_SCHEMAS


# --------------------------------------------------------------------------- #
# 7. The verification block: an authored page is run once over a synthetic copy
# --------------------------------------------------------------------------- #

_PAGE = '<span data-dashboard-field="open_prs"></span>\n<script>window.kirocrew.fields.open_prs;</script>'

#: The grammar the artifact store gives a slug (``artifact_store.rules._SLUG_RE``).
_STORE_SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?\Z")


def _html_body() -> dict[str, Any]:
    body = _package_body()
    body["view"] = {"html": _PAGE}
    return body


def _verdict(status: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "data": "synthetic",
        "errors": [],
        "reason": "",
        "notes": [],
        **extra,
    }


async def test_a_page_written_as_html_comes_back_with_its_verification(monkeypatch, store):
    """The agent learns what its page raised on a synthetic copy in the save's own reply.

    The runner is stubbed: what this proves is the route's half -- it is asked with the
    vetted caller's slug, crew and slot and with the CANONICAL package (what an apply
    would commit), a deadline no later than the check's time limit, its answer reaches
    the body, and the page stays saved whatever the answer is, so the fix is one more save.
    """
    from kiro_crew import dashboard_verify
    from kiro_crew.artifact_store import dashboard_package
    from kiro_crew.dashboard.handlers import member_dashboard_fields

    asked: dict[str, Any] = {}
    failed = _verdict(
        "failed",
        errors=[{"kind": "exception", "message": "TypeError: x", "line": 2, "column": 8}],
    )

    def _verify(slug: str, crew: str, slot: str, package: dict[str, Any], deadline=None):
        asked.update(slug=slug, crew=crew, slot=slot, package=package, deadline=deadline)
        return failed

    monkeypatch.setattr(member_dashboard_fields, "verify_package_page", _verify)
    # Decoding and validating an agent-sized package is not work for the event loop.
    on_the_loop: list[bool] = []
    real_parse = dashboard_package.parse_package

    def _parse(content: str):
        on_the_loop.append(threading.current_thread() is threading.main_thread())
        return real_parse(content)

    monkeypatch.setattr(dashboard_package, "parse_package", _parse)

    async with _client(monkeypatch) as client:
        resp = await _save(client, _html_body())
        payload = await resp.json()

    assert resp.status == 200, payload
    assert payload["verification"] == failed
    assert (asked["slug"], asked["crew"], asked["slot"]) == (SLUG, CREW, "slot-1")
    assert asked["package"]["view"] == {"html": _PAGE}
    assert asked["package"]["bound_to"] == f"crewmate:{SLUG}"
    # The check is over by one limit, counted from the save and not from the thread's start.
    assert asked["deadline"] <= time.monotonic() + dashboard_verify.RUN_TIMEOUT_SECONDS
    assert on_the_loop and not any(on_the_loop), "a package was parsed on the event loop"
    # STILL SAVED: a failed verdict does not undo the save, and nothing goes live.
    assert len(store.records) == 1


async def test_a_refused_page_is_not_run(monkeypatch, store):
    """The store's write gate answers first; a page that cannot be stored is not executed."""
    from kiro_crew.dashboard.handlers import member_dashboard_fields

    monkeypatch.setattr(
        member_dashboard_fields,
        "verify_package_page",
        lambda *_a, **_k: pytest.fail("ran a page the gate refused"),
    )
    body = _html_body()
    body["view"] = {"html": '<span data-dashboard-field="undeclared"></span>'}

    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        payload = await resp.json()

    assert resp.status == 400
    assert payload["code"] == "invalid_package"


class _FakeLivePage:
    """Stands in for ``LivePage``: records how it is built, armed and disarmed."""

    built: list[dict[str, Any]] = []
    calls: list[str] = []
    fail_on_arm = False
    #: What a read of the crewmate's data gives.
    record: dict[str, Any] = {"fields": {"open_prs": 4}, "version": 1}

    def __init__(self, slug: str, slot: str, package: dict[str, Any], send: Any, **kw: Any):
        type(self).built.append({"slug": slug, "slot": slot, "package": package, **kw})

    def arm(self) -> dict[str, Any]:
        type(self).calls.append("arm")
        if type(self).fail_on_arm:
            raise OSError("fold unreadable")
        return type(self).record

    def disarm(self) -> None:
        type(self).calls.append("disarm")


@pytest.fixture
def tab_stubs(monkeypatch):
    """What the tab's own side of the check decides, stubbed: the live page, the script
    grant and its audit row. The runner is left as it is."""
    from kiro_crew.dashboard.handlers import member_dashboard, member_dashboard_fields

    _FakeLivePage.built, _FakeLivePage.calls, _FakeLivePage.fail_on_arm = [], [], False
    monkeypatch.setattr(_FakeLivePage, "record", {"fields": {"open_prs": 4}, "version": 1})
    audited: list[dict[str, Any]] = []
    granted = member_dashboard._EgressDecision(True, "policy", "no_restriction")
    monkeypatch.setattr(member_dashboard_fields, "LivePage", _FakeLivePage)
    monkeypatch.setattr(member_dashboard, "_egress_decision", lambda *_a: granted)
    monkeypatch.setattr(
        member_dashboard, "_audit_page_script_grant", lambda **kw: audited.append(kw)
    )
    monkeypatch.setattr(member_dashboard, "_newest_unit", lambda _slot: "unit-9")
    return SimpleNamespace(module=member_dashboard_fields, audited=audited)


@pytest.fixture
def glue(tab_stubs, monkeypatch):
    """``verify_package_page`` with the live page, the grant and the runner stubbed.

    The runner stub asks for the record the way the real one does: once it is called, and
    by calling the function it was handed. ``ran`` keeps what it was given and whether the
    live page had been built by the time it was asked.
    """
    from kiro_crew import dashboard_verify

    ran: list[SimpleNamespace] = []
    # The cheap tests are the host's (not Linux, a check running, no memory); these tests
    # are about what the glue does once they pass.
    monkeypatch.setattr(dashboard_verify, "preflight", lambda: "")

    def _verify(html: str, read: Any, **kw: Any):
        built_before = len(_FakeLivePage.built)
        record = read() if callable(read) else read
        ran.append(
            SimpleNamespace(
                html=html, record=record, lazy=callable(read), built_before=built_before, **kw
            )
        )
        return dashboard_verify.Verification(
            dashboard_verify.STATUS_PASSED, data=dashboard_verify.DATA_SYNTHETIC
        )

    monkeypatch.setattr(dashboard_verify, "verify_page", _verify)
    tab_stubs.ran = ran
    return tab_stubs


@pytest.fixture
def browser(monkeypatch, tmp_path):
    """The real runner over a FAKE browser: a function that makes one with a scenario.

    The host's memory, its browser and its Python are the test's; the settle time is short,
    nothing is remembered of an earlier run, and the copy is made from a fixed seed so the
    document a browser is handed can be worked out.
    """
    import random

    from fake_devtools_browser import FakeBrowser

    from kiro_crew import dashboard_synth, dashboard_verify

    monkeypatch.setattr(platform_compat, "host_available_mib", lambda: 1 << 20)
    monkeypatch.setattr(dashboard_verify, "find_launcher_python", lambda: sys.executable)
    monkeypatch.setattr(dashboard_verify, "_SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(dashboard_verify, "_UNUSABLE", {})
    monkeypatch.setattr(dashboard_synth, "_fresh_rng", lambda: random.Random(1234))
    # A test that holds a browser ends it by releasing it, so no real limit may end the check
    # first, however slow the machine is. A test about a limit sets its own.
    monkeypatch.setattr(dashboard_verify, "RUN_TIMEOUT_SECONDS", 3600.0)
    monkeypatch.setattr(dashboard_verify, "REPLY_SECONDS", 3700.0)

    def _make(**scenario: Any) -> FakeBrowser:
        fake = FakeBrowser(tmp_path, **scenario)
        monkeypatch.setattr(dashboard_verify, "find_browser", lambda: str(fake.executable))
        return fake

    return _make


_PACKAGE = {"view": {"html": _PAGE}, "model": {"types": {}}, "theme": {"tokens": {}}}


def test_the_page_is_run_over_the_read_the_tab_is_served(glue):
    """The first paint's read: the tab's redaction, the crewmate's slot, then disarmed.

    The runner is handed a function, not the record: the read is taken once the runner
    holds the check's slot, so a save that is turned away reads nothing, and the runner
    is the only place the record goes.
    """
    from kiro_crew.dashboard.handlers import member_dashboard

    out = glue.module.verify_package_page(SLUG, CREW, "slot-1", _PACKAGE, 123.5)

    assert out is not None and out["status"] == "passed" and out["data"] == "synthetic"
    (ran,) = glue.ran
    assert ran.html == _PAGE
    assert ran.lazy and ran.built_before == 0, "the record was read before the runner asked"
    assert ran.record == {"fields": {"open_prs": 4}, "version": 1}
    # The package's own model tells the copy which words to keep and which maps are keyed.
    assert ran.model == _PACKAGE["model"]
    assert ran.deadline == 123.5
    (built,) = _FakeLivePage.built
    assert (built["slug"], built["slot"], built["session_unit"]) == (SLUG, "slot-1", "unit-9")
    assert built["redact"] is member_dashboard._page_safe
    assert _FakeLivePage.calls == ["arm", "disarm"]
    # Running the script here is the tab's own authorization, so it leaves the tab's own row.
    (row,) = glue.audited
    assert (row["member"], row["package_slug"], row["version"]) == (CREW, "(staged)", 0)
    assert row["decision"].granted is True
    # A name the store cannot give a package, so a real package keeps its own entry.
    assert not _STORE_SLUG.match(row["package_slug"])


#: Assembled at runtime, like the credential-shaped values in ``test_member_dashboard_routes``:
#: a literal of this shape in the diff is a finding against the PR whatever the code
#: around it is for. Do not join these back up.
_KEY = "AKIA" + "IOSFODNN7" + "EXAMPLE"


def _keyed_package() -> dict[str, Any]:
    """A page whose script holds a key-shaped string on its second line, and breaks if the
    string is not the length it was written with."""
    page = (
        '<span data-dashboard-field="open_prs"></span>\n'
        f'<script>var KEY = "{_KEY}"; if (KEY.length !== {len(_KEY)}) throw new Error("x");</script>'
    )
    return {**_PACKAGE, "view": {"html": page}}


def test_the_page_is_run_as_the_route_serves_it(glue):
    """The route runs a page's text through the secret filter before it composes it, so the
    runner is handed the text that filter gives, not the text the agent wrote.

    Without this a page that holds a string the filter rewrites runs here as written and
    passes, and breaks on the gateway alone.
    """
    from kiro_crew.dashboard.handlers import member_dashboard

    package = _keyed_package()
    written = package["view"]["html"]

    glue.module.verify_package_page(SLUG, CREW, "slot-1", package)

    (ran,) = glue.ran
    served = member_dashboard._template_text_safe(written)
    assert served != written, "the filter left the key in the page: the test proves nothing"
    assert ran.html == served
    assert _KEY not in ran.html


def test_the_filter_the_check_uses_is_the_one_the_route_has(glue, monkeypatch):
    """The check calls the route's functions by name, so a change to the filter reaches it."""
    from kiro_crew.dashboard.handlers import member_dashboard

    monkeypatch.setattr(member_dashboard, "_template_text_safe", lambda text: text.upper())
    monkeypatch.setattr(member_dashboard, "_manifest_text_safe", lambda model: {"seen": model})

    glue.module.verify_package_page(SLUG, CREW, "slot-1", _PACKAGE)

    (ran,) = glue.ran
    assert ran.html == _PAGE.upper()
    assert ran.model == {"seen": _PACKAGE["model"]}


def test_a_rewrite_is_told_to_the_agent_by_place_and_line_and_never_by_content(glue):
    package = _keyed_package()

    out = glue.module.verify_package_page(SLUG, CREW, "slot-1", package)

    assert out is not None and out["status"] == "passed"
    (note,) = out["notes"]
    assert "rewrote 1 place in it, on line 2" in note
    assert "The check works on the rewritten page" in note
    assert _KEY not in json.dumps(out) and "REDACTED" not in json.dumps(out)


def test_a_page_the_filter_leaves_alone_gets_no_such_note(glue):
    out = glue.module.verify_package_page(SLUG, CREW, "slot-1", _PACKAGE)

    assert out is not None and out["notes"] == []


def test_the_note_comes_before_the_runners_own_and_whatever_the_outcome(glue, monkeypatch):
    """The rewrite is a fact about the page and is told for a ``skipped`` check as well."""
    from kiro_crew import dashboard_verify

    def _skip(html: str, read: Any, **_kw: Any):
        return dashboard_verify.Verification(
            dashboard_verify.STATUS_SKIPPED, reason="no browser here", notes=("the runner's note",)
        )

    monkeypatch.setattr(dashboard_verify, "verify_page", _skip)

    out = glue.module.verify_package_page(SLUG, CREW, "slot-1", _keyed_package())

    assert out is not None and out["status"] == "skipped"
    assert out["notes"][1] == "the runner's note"
    assert "rewrote 1 place" in out["notes"][0]


def test_what_the_check_returns_has_crossed_the_redaction_the_tab_values_cross(glue, monkeypatch):
    """A page chooses the text of its errors, and can build a key-shaped string that no scan
    of its source saw. What goes back to the agent crosses the same pass the tab's values do."""
    from kiro_crew import dashboard_verify

    def _fail(html: str, read: Any, **_kw: Any):
        return dashboard_verify.Verification(
            dashboard_verify.STATUS_FAILED,
            (dashboard_verify.PageError("exception", f"Error: {_KEY}", 3, 7),),
            reason=f"stopped at {_KEY}",
            notes=(f"saw {_KEY}",),
            data=dashboard_verify.DATA_SYNTHETIC,
        )

    monkeypatch.setattr(dashboard_verify, "verify_page", _fail)

    out = glue.module.verify_package_page(SLUG, CREW, "slot-1", _PACKAGE)

    assert out is not None and _KEY not in json.dumps(out)
    (error,) = out["errors"]
    assert (error["kind"], error["line"], error["column"]) == ("exception", 3, 7)
    assert error["message"].startswith("Error: ") and out["status"] == "failed"
    assert out["data"] == "synthetic"


def test_the_model_reaches_the_copy_as_the_route_serves_it(glue):
    """The manifest the route sends is scrubbed in its strings and not in its keys."""
    package = {
        **_PACKAGE,
        "model": {"types": {"state": {"type": "enum", "choices": ["ok", f"note {_KEY}"]}}},
    }

    glue.module.verify_package_page(SLUG, CREW, "slot-1", package)

    (ran,) = glue.ran
    assert _KEY not in json.dumps(ran.model)
    assert ran.model["types"]["state"]["choices"][0] == "ok"
    assert list(ran.model["types"]) == ["state"], "a name was rewritten"


def test_a_block_package_is_not_run(glue):
    assert glue.module.verify_package_page(SLUG, CREW, "slot-1", {"view": {"blocks": []}}) is None
    assert _FakeLivePage.built == []


def test_a_page_the_tab_would_mount_without_script_is_not_run(glue, monkeypatch):
    """An owner whose egress is restricted has asked that the page's script not run."""
    from kiro_crew.dashboard.handlers import member_dashboard

    withheld = member_dashboard._EgressDecision(False, "profile", "profile_restricts_egress")
    monkeypatch.setattr(member_dashboard, "_egress_decision", lambda *_a: withheld)

    out = glue.module.verify_package_page(SLUG, CREW, "slot-1", _PACKAGE)

    assert out is not None and out["status"] == "skipped"
    assert "without script" in out["reason"]
    assert glue.ran == [] and _FakeLivePage.built == []
    # The refusal is a decision too, and is recorded as one.
    (row,) = glue.audited
    assert row["decision"].granted is False and row["decision"].reason == "profile_restricts_egress"


def test_a_session_that_is_not_a_crewmate_has_its_page_left_unrun(glue):
    """No tab and no owner to ask: the grant decision would be one nothing else makes."""
    out = glue.module.verify_package_page(SLUG, "", "slot-1", _PACKAGE)

    assert out is not None and out["status"] == "skipped"
    assert "not a crewmate" in out["reason"]
    assert glue.ran == [] and _FakeLivePage.built == [] and glue.audited == []


def test_a_check_that_cannot_start_reads_nothing_and_audits_nothing(glue, monkeypatch):
    """The cheap refusals come first, so a save that will be skipped costs a read of nothing
    and takes no decision that nothing then acts on."""
    from kiro_crew import dashboard_verify

    busy = "busy: another page check is running, so this one was not started"
    monkeypatch.setattr(dashboard_verify, "preflight", lambda: busy)

    out = glue.module.verify_package_page(SLUG, CREW, "slot-1", _PACKAGE)

    assert out is not None and out["status"] == "skipped" and out["reason"] == busy
    assert glue.ran == [] and _FakeLivePage.built == [] and glue.audited == []


def test_a_read_that_fails_is_a_skip_and_the_page_is_still_disarmed(glue):
    """The runner never raises, but whatever the read does, the live page is let go."""
    _FakeLivePage.fail_on_arm = True

    out = glue.module.verify_package_page(SLUG, CREW, "slot-1", _PACKAGE)

    assert out is not None and out["status"] == "skipped"
    assert "could not run" in out["reason"]
    assert _FakeLivePage.calls == ["arm", "disarm"]
    assert glue.ran == []


def test_the_tool_hands_the_verification_to_the_agent(monkeypatch):
    from kiro_crew import mcp_panel

    monkeypatch.setattr(mcp_panel, "_strict_session_key", lambda: ("sk", ""))
    monkeypatch.setattr(
        mcp_panel,
        "_post",
        lambda *_a, **_k: {
            "saved": {"fields": 2, "blocks": 0},
            "verification": _verdict(
                "failed",
                errors=[{"kind": "exception", "message": "Error: boom", "line": 3, "column": 1}],
            ),
        },
    )

    out = mcp_panel._call_tool_inner("dashboard_save", _html_body())

    assert "verification: failed" in out
    assert "Error: boom (view.html line 3, column 1)" in out


def test_the_tool_description_tells_the_agent_about_the_check():
    from kiro_crew import mcp_panel

    tool = next(t for t in mcp_panel._tool_definitions() if t["name"] == "dashboard_save")

    assert "`verification`" in tool["description"]
    assert "offline" in tool["description"]
    # A page that stops at an unfetched library is not a page to fix.
    assert "`skipped`, not `failed`" in tool["description"]
    # The page never sees the record, and the agent is told what it ran over instead.
    assert "SYNTHETIC copy" in tool["description"]
    assert "never sees the record" in tool["description"]
    assert "a key is kept only if your page spells it out" in tool["description"]
    # The page is checked as the tab is served it, and the agent is told what that means.
    assert "secret filter rewrites" in tool["description"]
    assert "which line the first is on" in tool["description"]
    # The ways a check does not finish are told too, with the longest the save waits.
    assert "`timed_out`" in tool["description"]
    assert "One page is checked at a time" in tool["description"]
    assert "never more than about twelve" in tool["description"]


#: What the secret filter writes in place of what it takes out.
_MARK = "[REDACTED: credential]"

LINUX_ONLY = pytest.mark.skipif(
    not platform_compat.IS_LINUX, reason="the page check runs on Linux only"
)


def _read_payload() -> dict[str, Any]:
    """A read in the shape the tab is served: what the live page gives the check."""
    from kiro_crew import dashboard_frame

    return dashboard_frame.read_payload({"open_prs": 4}, agentic=["open_prs"], seq=1)


@LINUX_ONLY
async def test_a_saved_page_is_run_end_to_end_through_the_route(
    monkeypatch, store, browser, tmp_path
):
    """The route, the real live page and the runner, with only the browser faked.

    Nothing between the save and the browser is stubbed: the route stages the page, the
    glue arms a real ``LivePage`` for the crewmate (the grant, the redaction, the feed),
    the document is composed from a copy of that read, and the browser is handed it. What
    comes back is the error the browser reported, placed on the line of the agent's own
    page, and the verdict says the page ran over a synthetic copy.
    """
    fake = browser(
        events=[
            {
                "type": "exception",
                "find": "boom()",
                "description": "ReferenceError: boom is not defined",
            }
        ],
    )
    body = _package_body()
    body["view"] = {
        "html": '<span data-dashboard-field="open_prs"></span>\n<script>\n  boom();\n</script>'
    }

    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        payload = await resp.json()

    assert resp.status == 200, payload
    verification = payload["verification"]
    assert verification["status"] == "failed", verification
    assert verification["data"] == "synthetic"
    assert [(e["kind"], e["message"], e["line"]) for e in verification["errors"]] == [
        ("exception", "ReferenceError: boom is not defined", 3)
    ]
    (served,) = fake.records("fulfill")
    document = served["document"]
    # The tab's first paint: the live page's read, in the frame's own document.
    assert 'id="kirocrew-dashboard-data"' in document
    assert '"generation"' in document and '"missing"' in document
    assert "boom();" in document
    assert len(store.records) == 1


@LINUX_ONLY
async def test_an_error_a_page_builds_from_pieces_is_redacted_in_the_reply(
    monkeypatch, store, tab_stubs, browser
):
    """The page joins two literals at run time and throws the result. The browser reports it,
    and the save's reply does not carry it."""
    browser(
        events=[
            {
                "type": "exception",
                "find": "boom()",
                "description": f"Error: {_KEY}",
            }
        ]
    )
    monkeypatch.setattr(_FakeLivePage, "record", _read_payload())
    body = _package_body()
    body["view"] = {
        "html": '<span data-dashboard-field="open_prs"></span>\n<script>\n  boom();\n</script>'
    }

    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        raw = await resp.text()

    verification = json.loads(raw)["verification"]
    assert verification["status"] == "failed", verification
    assert [(e["kind"], e["line"]) for e in verification["errors"]] == [("exception", 3)]
    assert _KEY not in raw


@LINUX_ONLY
async def test_the_browser_of_a_saved_page_is_given_a_copy_and_never_the_record(
    monkeypatch, store, tab_stubs, browser
):
    """From the save to the browser: the record is read, copied and let go there.

    Every value the crewmate's read held is looked for in everything the browser received,
    and none is there; the copy has the read's field names, so a page binds as it will.
    """
    from kiro_crew import dashboard_frame

    secrets = ["ZXQ-LEDGER-7781", "it_5e1d9c0b", "https://intranet.example.test/secret-path"]
    record = dashboard_frame.read_payload(
        {"open_prs": 4821, "headline": secrets[0], "owner": secrets[1], "link": secrets[2]},
        agentic=["open_prs", "headline"],
        seq=914827,
    )
    monkeypatch.setattr(_FakeLivePage, "record", record)
    fake = browser()

    async with _client(monkeypatch) as client:
        resp = await _save(client, _html_body())
        payload = await resp.json()

    assert payload["verification"]["status"] == "passed", payload["verification"]
    assert payload["verification"]["data"] == "synthetic"
    everything = (fake.directory / "received.jsonl").read_text()
    for value in (*secrets, "4821", "914827"):
        assert value not in everything, value
    (served,) = fake.records("fulfill")
    for name in ("open_prs", "headline", "owner", "link"):
        assert f'"{name}"' in served["document"], name


@LINUX_ONLY
async def test_a_page_that_breaks_only_on_text_the_filter_rewrites_fails_and_says_why(
    monkeypatch, store, tab_stubs, browser
):
    """The page the tab runs is the filter's output, so the check runs that.

    The fake browser reports an error only if the document it is handed holds the filter's
    mark, as a page whose script depends on a string the filter rewrites does. Checked on
    the page as written it would pass.
    """
    fake = browser(
        events=[
            {
                "type": "exception",
                "find": _MARK,
                "description": "Error: the key was rewritten",
                "if_present": True,
            }
        ]
    )
    monkeypatch.setattr(_FakeLivePage, "record", _read_payload())
    keyed = _package_body()
    keyed["view"] = _keyed_package()["view"]

    async with _client(monkeypatch) as client:
        broken = await (await _save(client, keyed)).json()
        clean = await (await _save(client, _html_body())).json()

    verification = broken["verification"]
    assert verification["status"] == "failed", verification
    assert [(e["kind"], e["message"], e["line"]) for e in verification["errors"]] == [
        ("exception", "Error: the key was rewritten", 2)
    ]
    assert "rewrote 1 place in it, on line 2" in verification["notes"][0]
    assert clean["verification"]["status"] == "passed" and clean["verification"]["notes"] == []
    served, _ = fake.records("fulfill")
    assert _MARK in served["document"] and _KEY not in served["document"]
    assert _KEY not in (fake.directory / "received.jsonl").read_text()
    assert _KEY not in json.dumps(broken)


@pytest.fixture(scope="module")
def real_browser():
    """Skip unless this host has a Chromium that runs a plain page to the end.

    A browser that is installed and cannot run (a container without the sandbox's
    namespaces, a start that hangs) comes back ``skipped`` or ``timed_out``, and only a
    ``passed`` plain page says the tests below have a browser to test with.
    """
    if not platform_compat.IS_LINUX:
        pytest.skip("the page check runs on Linux only")
    from kiro_crew import dashboard_verify

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(dashboard_verify, "find_launcher_python", lambda: sys.executable)
        patch.setattr(platform_compat, "host_available_mib", lambda: 1 << 20)
        probe = dashboard_verify.verify_page("<p>x</p>", _read_payload())
    if probe.status != dashboard_verify.STATUS_PASSED:
        pytest.skip(f"no usable browser here: {probe.status}: {probe.reason or probe.errors}")


def _skip_if_the_host_failed(status: str, reason: str) -> None:
    """Skip when a check says this host's browser did not run the page.

    The fixture's plain page ran, and a later start can still fail on a host whose browser
    is unreliable. That is the host's doing, and the test below says nothing about it.
    """
    if status == "timed_out" or (
        status == "skipped" and reason.startswith("the browser could not")
    ):
        pytest.skip(f"no usable browser here: {status}: {reason}")


@LINUX_ONLY
async def test_a_real_page_that_breaks_only_on_what_the_filter_rewrites_is_failed(
    monkeypatch, store, tab_stubs, real_browser
):
    """The failure that shows on the gateway and nowhere else, in a real browser.

    The script checks the length of a string the filter rewrites. Run as the agent wrote it,
    the page passes; saved, it is failed, at the line the script threw on, and the reply
    says where the filter touched it.
    """
    from kiro_crew import dashboard_verify

    monkeypatch.setattr(dashboard_verify, "find_launcher_python", lambda: sys.executable)
    monkeypatch.setattr(platform_compat, "host_available_mib", lambda: 1 << 20)
    record = _read_payload()
    monkeypatch.setattr(_FakeLivePage, "record", record)
    page = (
        '<span data-dashboard-field="open_prs"></span>\n'
        "<script>\n"
        f'  var KEY = "{_KEY}";\n'
        f'  if (KEY.length !== {len(_KEY)}) {{ throw new Error("the key was rewritten"); }}\n'
        "</script>"
    )
    as_written = await asyncio.to_thread(dashboard_verify.verify_page, page, record)
    _skip_if_the_host_failed(as_written.status, as_written.reason)
    assert as_written.status == dashboard_verify.STATUS_PASSED, as_written
    body = _package_body()
    body["view"] = {"html": page}

    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        payload = await resp.json()

    verification = payload["verification"]
    _skip_if_the_host_failed(verification["status"], verification.get("reason", ""))
    assert verification["status"] == "failed", verification
    assert [(e["kind"], e["line"]) for e in verification["errors"]] == [("exception", 4)]
    assert "the key was rewritten" in verification["errors"][0]["message"]
    assert "rewrote 1 place in it, on line 3" in verification["notes"][0]


# --------------------------------------------------------------------------- #
# 8. What the check costs the gateway
# --------------------------------------------------------------------------- #


@LINUX_ONLY
async def test_the_event_loop_keeps_turning_while_a_page_is_being_checked(
    monkeypatch, store, tab_stubs, browser
):
    """The check, with the browser holding the page, runs on a thread.

    If any part of it ran on the loop, the loop could not poll for the browser's page, let
    alone turn again, and the save would be over before the first of these assertions.
    """
    fake = browser(hold=True)
    async with _client(monkeypatch) as client:
        saving = asyncio.ensure_future(_save(client, _html_body()))
        try:
            await async_wait_until(
                lambda: len(fake.records("fulfill")) == 1,
                off_loop=True,
                describe=lambda: f"the browser never got the page: {fake.records('start')}",
            )
            for _ in range(100):
                await asyncio.sleep(0)
            assert not saving.done(), "the save was answered while the browser held the page"
        finally:
            fake.release()
        resp = await await_until(saving, what="the held save being answered")
        payload = await resp.json()

    assert payload["verification"]["status"] == "passed", payload["verification"]


@LINUX_ONLY
async def test_two_saves_at_once_start_one_browser(monkeypatch, store, tab_stubs, browser):
    """One check runs at a time across the gateway; the other save is told so and goes on."""
    fake = browser(hold=True)
    async with _client(monkeypatch) as client:
        saves = [asyncio.ensure_future(_save(client, _html_body())) for _ in range(2)]
        try:
            # One has the slot and its browser holds the page; the other is already answered.
            await async_wait_until(
                lambda: len(fake.records("fulfill")) == 1 and any(s.done() for s in saves),
                off_loop=True,
                describe=lambda: f"started {len(fake.records('start'))}, done {[s.done() for s in saves]}",
            )
            (turned_away,) = [s for s in saves if s.done()]
            busy = (await (await turned_away).json())["verification"]
            assert busy["status"] == "skipped" and busy["reason"].startswith("busy"), busy
            assert len(fake.records("start")) == 1, "the second save started a browser"
        finally:
            fake.release()
        (running,) = [s for s in saves if s is not turned_away]
        answered = await await_until(running, what="the released save being answered")
        ran = (await answered.json())["verification"]

    assert ran["status"] == "passed", ran


def _wait_ends_when(started):
    """A stand-in for the save's wait that ends as soon as ``started()`` is true.

    The real wait ends on a timer. This one ends when the check has done what the test needs
    it to have done, so the order of the two is the test's and not the scheduler's. Like the
    real wait it leaves the check running in its thread.
    """

    async def wait(work):
        task = asyncio.ensure_future(work)
        try:
            await async_wait_until(started, off_loop=True)
            raise TimeoutError
        finally:
            task.cancel()

    return wait


async def test_a_save_stops_waiting_for_a_check_that_does_not_come_back(
    monkeypatch, store, tab_stubs
):
    """The reply is not held for a check that does not return.

    The check is a function that does not return until the test lets it, and the save's wait
    ends once the check has started, so the save is answered while the check is running.
    """
    from kiro_crew.dashboard.handlers import member_dashboard_fields

    running = threading.Event()
    release = threading.Event()

    def _stuck(slug: str, crew: str, slot: str, package: dict[str, Any], deadline=None):
        running.set()
        release.wait(60)
        return None

    monkeypatch.setattr(member_dashboard_fields, "verify_package_page", _stuck)
    monkeypatch.setattr(routes, "_await_check", _wait_ends_when(running.is_set))

    async with _client(monkeypatch) as client:
        try:
            resp = await _save(client, _html_body())
            payload = await resp.json()
        finally:
            release.set()

    assert resp.status == 200, payload
    assert running.is_set(), "the check never started"
    assert payload["verification"]["status"] == "timed_out"
    assert "stopped waiting for it" in payload["verification"]["reason"]
    assert payload["saved"], "the page is saved whatever the check did"


async def test_the_save_waits_for_a_check_no_longer_than_the_bound(monkeypatch):
    """The one wait is ``dashboard_verify.REPLY_SECONDS``, read when the save waits."""
    from kiro_crew import dashboard_verify

    asked: list[float | None] = []

    async def _fake_wait_for(work, timeout=None):
        asked.append(timeout)
        return await work

    # Only in the route module's own namespace, so nothing else that waits is touched.
    monkeypatch.setattr(routes, "asyncio", SimpleNamespace(wait_for=_fake_wait_for))
    monkeypatch.setattr(dashboard_verify, "REPLY_SECONDS", 4.5)

    async def done():
        return {"status": "passed"}

    assert await routes._await_check(done()) == {"status": "passed"}
    assert asked == [4.5]


@LINUX_ONLY
async def test_a_check_the_save_stopped_waiting_for_still_holds_its_place(
    monkeypatch, store, tab_stubs, browser
):
    """The save is answered, the check goes on in its thread and keeps the one place, so a
    second save is turned away rather than started on top of it."""
    from kiro_crew import dashboard_verify

    fake = browser(hold=True)
    real_wait = routes._await_check
    monkeypatch.setattr(
        routes, "_await_check", _wait_ends_when(lambda: len(fake.records("fulfill")) == 1)
    )

    async with _client(monkeypatch) as client:
        try:
            # The wait ends once the browser holds the page, which is while the check runs.
            first = (await (await _save(client, _html_body())).json())["verification"]
            monkeypatch.setattr(routes, "_await_check", real_wait)
            second = (await (await _save(client, _html_body())).json())["verification"]
            starts = len(fake.records("start"))
        finally:
            fake.release()
        await async_wait_until(lambda: not dashboard_verify._SLOT.locked())

    assert first["status"] == "timed_out" and "stopped waiting for it" in first["reason"]
    assert second["status"] == "skipped" and second["reason"].startswith("busy"), second
    assert starts == 1, "the second save started a browser"


@LINUX_ONLY
async def test_a_save_whose_limit_has_passed_starts_no_browser_and_says_timed_out(
    monkeypatch, store, tab_stubs, browser
):
    """The limit is counted from the save, so a check that gets its turn after it is over
    does not start a browser. (Killing a browser that outlives the limit is the runner's,
    and is tested there.)"""
    from kiro_crew import dashboard_verify

    fake = browser()
    monkeypatch.setattr(dashboard_verify, "RUN_TIMEOUT_SECONDS", 0.0)

    async with _client(monkeypatch) as client:
        resp = await _save(client, _html_body())
        payload = await resp.json()

    verification = payload["verification"]
    assert verification["status"] == "timed_out", verification
    assert "no time left" in verification["reason"]
    assert fake.records("start") == []
    assert not dashboard_verify._SLOT.locked()


def test_only_a_save_of_a_written_page_can_start_a_check():
    """The browser is for ``dashboard_save`` of a page written as ``view.html`` and for
    nothing else: not a ``dashboard_write``, a preview, an apply, or the load of a page.

    Every module that names the runner or the glue is listed, and within the route module
    the one function that starts a check is the save.
    """
    from source_corpus import iter_candidate_sources, src_root

    users = sorted(
        path.relative_to(src_root()).as_posix()
        for path, _text in iter_candidate_sources(
            require_any=("verify_package_page", "verify_page", "dashboard_verify")
        )
    )
    assert users == [
        "dashboard/handlers/agent_panel.py",
        "dashboard/handlers/member_dashboard_fields.py",
        "dashboard_verify.py",
    ]
    tree = ast.parse(Path(routes.__file__).read_text(encoding="utf-8"))
    starters = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        for call in ast.walk(fn)
        if isinstance(call, ast.Call) and getattr(call.func, "id", None) == "_page_check"
    }
    assert starters == {"api_dashboard_save"}


async def test_a_skipped_run_says_so_and_the_gateway_checks_shapes_and_schemes(monkeypatch, store):
    """No browser on the host: the reply says the page was NOT run, and the two checks
    the gateway does itself still come back -- a list bound to a text cell, and a theme
    that fixes the text colour for one scheme only (round-5 Cove)."""
    from kiro_crew.dashboard.handlers import member_dashboard_fields

    skipped = _verdict("skipped", reason="no browser on this host", data=None)
    monkeypatch.setattr(member_dashboard_fields, "verify_package_page", lambda *_a, **_k: skipped)
    body = _html_body()
    body["model"]["types"]["open_prs"] = {"type": "array", "source": {"agentic": True}}
    body["theme"] = {"tokens": {"--panel-fg": "#eeeeee"}}

    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        payload = await resp.json()

    assert resp.status == 200, payload
    note = payload["verification"]["note"]
    assert "NOT run" in note and "no browser on this host" in note
    assert [w.split(" ", 3)[:3] for w in payload["shape_warnings"]] == [["open_prs", "is", "array"]]
    assert len(payload["theme_warnings"]) == 1
    assert {"field": "open_prs", "source": "manual", "type": "array"} in payload["fields"]
