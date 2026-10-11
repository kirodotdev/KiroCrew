"""Tests for ``dashboard_save`` -- the call that stores a composed page.

The property guarded hardest here is the one a save introduces that no sibling
panel tool had: a save MINTS AN ARTIFACT, and the artifact's ``bound_to`` is what
decides whose Dashboard tab will render it. So the binding is derived from the
vetted caller on the server and is never read out of the body -- a caller that
sends one is refused, and a page already bound to another crewmate is never
written over.

The second property is that nothing here reimplements the store's own write gate.
Every save lands through ``ArtifactStore.create`` / ``.update``, which run
``canonical_package_content`` and therefore the package's size, surrogate, closed
-catalog and store-ownership guards. The refusal tests below prove that by sending
packages each guard refuses and asserting the save answers 400 rather than storing
them.
"""

from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.artifact_store.dashboard_package import DASHBOARD_KIND, parse_package
from kiro_crew.dashboard.handlers import agent_panel as routes

pytestmark = pytest.mark.asyncio

CREW = "fleet-crew"
SLUG = "fleet-crew"
OTHER_SLUG = "other-crew"


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
        "view": {
            "blocks": [
                {"id": "lead", "type": "stat", "fields": ["open_prs"], "title": "Open"},
                {"id": "note", "type": "list", "fields": ["headline"]},
            ]
        },
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
        #: Records the LISTING never showed, because their metadata will not read.
        #: Invisible to the scan rather than skipped by it, which is why the store
        #: reports them separately.
        self.unlistable = 0

    # -- the surface the handler uses -------------------------------------- #

    def list(self, *, kind: str | None = None, **_kw: Any) -> list[SimpleNamespace]:
        return [r for r in self.records.values() if kind is None or r.kind == kind]

    def unreadable_record_count(self) -> int:
        """How many stored records the LISTING itself could not show.

        A different kind of blindness from :attr:`unreadable`: that one is a record
        the listing named and ``get`` then refused, while this is a record whose
        metadata will not read at all, so ``list`` skips it and its kind is never
        known. The binding lookup asks this once its scan has found nothing,
        because a record it never saw could be the one bound here.
        """
        return self.unlistable

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
            slug=slug, name=name, content=content, kind=kind or "markdown", version=1
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


def _page_slug(store: Any, slug: str = SLUG) -> str:
    """The slug of the page bound to *slug*, resolved the way the read path does.

    The save response carries no slug -- an agent addresses its page by its own
    identity and never by an artifact id -- so a test that wants the stored record
    asks the binding, which is also the question the reader asks.
    """
    from kiro_crew.artifact_store.dashboard_package import resolve_bound_slug

    found = resolve_bound_slug(f"crewmate:{slug}", store=store)
    assert found is not None, f"no page is bound to crewmate:{slug}"
    return found


async def _save(client: TestClient, body: dict[str, Any]):
    return await client.post("/api/agent-panel/dashboard/save", json=body)


# --------------------------------------------------------------------------- #
# 1. A saved page round-trips
# --------------------------------------------------------------------------- #


async def test_a_saved_page_is_read_back_as_the_package_that_was_sent(monkeypatch, store):
    """Save, then read: the stored package carries the model, view and theme."""
    async with _client(monkeypatch) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 200, await resp.text()
        saved = (await resp.json())["saved"]

    stored = parse_package(store.get(_page_slug(store)).content)
    assert stored["kind"] == DASHBOARD_KIND
    assert stored["bound_to"] == f"crewmate:{SLUG}"
    assert sorted(stored["model"]["types"]) == ["headline", "open_prs"]
    assert [b["id"] for b in stored["view"]["blocks"]] == ["lead", "note"]
    assert stored["theme"]["tokens"] == {"--accent": "oklch(0.7 0.15 150)"}
    assert saved["version"] == 1
    assert set(saved) == {"version", "versioned", "fields", "blocks"}


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


async def test_a_second_save_of_the_same_layout_creates_no_new_version(monkeypatch, store):
    async with _client(monkeypatch) as client:
        first = await _save(client, _package_body())
        assert first.status == 200
        again = await _save(client, _package_body())
        assert again.status == 200
        second = (await again.json())["saved"]

    assert second["version"] == 1
    assert second["versioned"] is False
    assert len(store.records) == 1


async def test_a_changed_layout_does_cut_a_version(monkeypatch, store):
    """The control for the test above: a real change must still be versioned."""
    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        changed = _package_body()
        changed["theme"]["tokens"]["--accent"] = "oklch(0.6 0.2 20)"
        resp = await _save(client, changed)
        assert resp.status == 200
        saved = (await resp.json())["saved"]

    assert saved["version"] == 2
    assert saved["versioned"] is True


# --------------------------------------------------------------------------- #
# 3. An invalid package is refused with a 400-class error
# --------------------------------------------------------------------------- #


async def test_an_unknown_block_type_is_refused(monkeypatch, store):
    body = _package_body()
    body["view"]["blocks"][0]["type"] = "hologram"
    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400
        payload = await resp.json()
    assert "hologram" in payload["error"]
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
    """A package whose block title is an unpaired ``\\ud800``, as raw JSON bytes.

    Raw rather than through ``json=``: the escape has to reach the handler AS
    JSON, because it is six ASCII characters until the decoder turns it into the
    single code point no UTF-8 encoder accepts.
    """
    body = _package_body()
    body["view"]["blocks"][0]["title"] = "LONE"
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


async def test_a_lone_surrogate_is_refused_by_the_real_store_too(monkeypatch, tmp_path):
    """The same body against ``ArtifactStore`` itself, not the stand-in above.

    The stand-in cannot answer this one. It calls the dashboard gate directly, so
    it answers 400 whatever the real store does; the real store's FIRST call is
    the generic ``_validate_content``, which measures content by encoding it, and
    content holding an unpaired surrogate cannot be encoded at all. That function
    now refuses it as an ``ArtifactValidationError``, so this route's 400 arm
    catches it. Pinned end to end here, and at the store itself in
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
    surrogate["view"] = {"blocks": [dict(pkg["view"]["blocks"][0], title="\ud800")]}
    bad = json.dumps(surrogate, ensure_ascii=False)
    assert "\ud800" in bad
    with pytest.raises(ArtifactValidationError) as update_refusal:
        store.update(created.slug, content=bad)
    assert "surrogate" in str(update_refusal.value)

    with pytest.raises(ArtifactValidationError) as create_refusal:
        store.create(name="other", content=bad, kind=DASHBOARD_KIND, source="dashboard")
    assert "surrogate" in str(create_refusal.value)

    # The live package is untouched by the refused update, and the control: the
    # same bytes without the surrogate are still accepted, so the refusal above
    # is the surrogate's and not the whole package being rejected.
    assert parse_package(store.get(created.slug).content)["bound_to"] == f"crewmate:{SLUG}"
    assert store.update(created.slug, content=json.dumps(pkg)) is not None


async def test_a_save_is_refused_when_the_binding_cannot_be_proven(monkeypatch, store):
    """An unreadable dashboard record refuses the save; it never mints a second page.

    The read path resolves one binding to ONE slug. So "I could not tell whether
    this crewmate has a package" must not be answered as "it has none": that
    would create a second artifact under the same binding, and the tab would then
    render whichever of the two it met first.
    """
    mine = _package_body()
    mine["kind"] = DASHBOARD_KIND
    mine["bound_to"] = f"crewmate:{SLUG}"
    existing = store.create(name="page", content=json.dumps(mine), kind=DASHBOARD_KIND)
    store.unreadable.add(existing.slug)

    async with _client(monkeypatch) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 400, await resp.text()
        assert "could not be read" in (await resp.json())["error"]

    assert list(store.records) == [existing.slug]
    assert store.updates == []


async def test_a_save_is_refused_when_a_record_never_reached_the_listing(monkeypatch, store):
    """The other blindness: a record whose metadata will not read is never listed.

    The scan cannot skip what it never saw, so the store reports the count and the
    lookup refuses on it. Same consequence for this route as an unopenable record
    -- answering "no package" would mint a second one under this binding -- so the
    save must be refused here too, with nothing created.
    """
    store.unlistable = 1
    async with _client(monkeypatch) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 400, await resp.text()
        assert "will not read" in (await resp.json())["error"]

    assert store.records == {}
    assert store.updates == []


async def test_two_overlapping_first_saves_leave_one_page(monkeypatch, store):
    """Two first saves racing for one binding must not both create.

    The store locks each write on its own, which is a different guarantee: a
    crewmate's FIRST save asks whether a page exists and then creates one, and
    ``create`` disambiguates two slugs rather than colliding them, so two lookups
    that both answer absent produce two artifacts under one binding. The read
    path resolves a binding to ONE slug, so the loser's page and its whole
    version history would be invisible rather than merged.

    NO TIMEOUT DECIDES THE PASS. The first lookup parks until the second request
    CONTENDS for the binding lock, and that contention is the only thing that
    releases it, so the test cannot pass by a wait expiring while the machine is
    busy. If the signal never comes the test fails and says so, which is also
    exactly what happens when the lock is removed: with no lock there is nothing
    to contend for, so nothing releases the parked lookup. The wait therefore
    carries a bound only so a real defect reports rather than hangs.

    ``_page_slug`` asserts the invariant the reader depends on: exactly one page
    bound here, because two make its own resolver's answer arbitrary.
    """
    import asyncio as _asyncio

    contended = threading.Event()
    attempts = {"n": 0}
    counting = threading.Lock()
    real_lock = routes._page_save_lock
    real_find = routes._find_page

    class _WatchedLock:
        """The real lock, announcing each attempt to take it BEFORE it blocks.

        Announcing before acquiring is what makes this deadlock-free: the parked
        first lookup is holding the lock, so a signal sent after acquisition
        could never arrive.
        """

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __enter__(self) -> "_WatchedLock":
            with counting:
                attempts["n"] += 1
                if attempts["n"] >= 2:
                    contended.set()
            self._inner.acquire()
            return self

        def __exit__(self, *_exc: Any) -> bool:
            self._inner.release()
            return False

    def _watched(binding: str) -> Any:
        return _WatchedLock(real_lock(binding))

    def _racing_find(st: Any, binding: str) -> Any:
        found = real_find(st, binding)
        if found is None:
            # Only the FIRST-save path parks, and only until somebody else wants
            # this binding. A caller that already sees a page has no race to lose.
            assert contended.wait(timeout=30), (
                "the second save never contended for the binding lock, so nothing "
                "serialized the two: the lookup and the write are not under one lock"
            )
        return found

    monkeypatch.setattr(routes, "_page_save_lock", _watched)
    monkeypatch.setattr(routes, "_find_page", _racing_find)

    async with _client(monkeypatch) as client:
        first, second = await _asyncio.gather(
            _save(client, _package_body()), _save(client, _package_body())
        )
        assert first.status == 200, await first.text()
        assert second.status == 200, await second.text()

    assert len(store.records) == 1, "a race created a second page under one binding"
    assert _page_slug(store) in store.records
    # One create and one update, in that order: the second save found the first
    # one's page rather than minting its own.
    assert len(store.updates) == 1


async def test_the_store_is_built_off_the_event_loop(monkeypatch, store):
    """``get_default_store`` constructs the singleton, so it must not run on the loop.

    Building it resolves paths, runs the sensitive-path checks and creates
    directories, and slow storage doing that on the gateway loop stalls every
    chat and heartbeat. Asserted by thread identity rather than by reading the
    source, so moving the call back onto the loop fails here.
    """
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _store_from(*_a: Any, **_k: Any) -> _Store:
        seen.append(threading.get_ident())
        return store

    monkeypatch.setattr(routes, "_dashboard_artifact_store", _store_from)
    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200

    assert seen, "the route never asked for a store"
    assert loop_thread not in seen, "the artifact store was built on the event loop"


async def test_the_history_session_is_resolved_off_the_event_loop(monkeypatch, store):
    """``_history_session`` walks the slot's succession chain, so it is thread work.

    It reads the crew-log root and each unit's header, and the three sibling
    instance routes all call it inside a function handed to ``asyncio.to_thread``.
    Pinned by thread identity rather than by reading the source, and exercised on
    the UPDATE branch, because that is the only branch that needs it -- a create
    records no history session.
    """
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _history_from(_slot: str) -> str:
        seen.append(threading.get_ident())
        return "chat-1"

    monkeypatch.setattr(routes, "_history_session", _history_from)
    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        changed = _package_body()
        changed["theme"]["tokens"]["--accent"] = "oklch(0.3 0.02 10)"
        assert (await _save(client, changed)).status == 200

    assert seen, "the update never resolved a history session"
    assert loop_thread not in seen, "the history session was resolved on the event loop"
    assert store.updates and store.updates[0].get("session_id") == "chat-1"


async def test_a_saved_package_becomes_the_model_the_write_path_checks(monkeypatch, tmp_path):
    """The save's whole point, end to end: the write path adopts the saved Model.

    Everything else in this file proves the artifact is stored correctly. This one
    proves it is CONSUMED -- that after a save, ``read_package_model`` answers for
    this crewmate with the package's own fields rather than a template's, which is
    what makes ``dashboard_write`` type-check an agent's value against the layout
    it just declared.

    Driven through the real ``ArtifactStore`` and the real reader, because a
    stand-in on either side would prove only that the two agree with the stub.
    """
    from kiro_crew import artifacts as artifacts_mod
    from kiro_crew import dashboard_package as reader
    from kiro_crew.artifacts import ArtifactStore

    real = ArtifactStore(tmp_path / "artifacts")
    # Patched at the SOURCE, not on either consumer: the reader imports
    # ``get_default_store`` inside the function that uses it, so a module attribute
    # set on ``dashboard_package`` would never be seen. One patch here therefore
    # puts the route and the reader on the same store, which is the whole point --
    # two stores would make them agree about nothing.
    monkeypatch.setattr(artifacts_mod, "get_default_store", lambda: real)

    # THE CONTROL IS THE FIELD SET, not the state. An unadopted crewmate already
    # reads ``live``, because the template path stands behind the package path for
    # the empty state and substitutes the DEFAULT page -- so the state says
    # nothing about whose layout answered. What changes at the save is which
    # fields a write is checked against, and that is the thing worth pinning.
    before = reader.read_package_model(SLUG)
    assert before.model is not None
    assert sorted(before.model.manifest.fields) != ["headline", "open_prs"]
    default_fields = sorted(before.model.manifest.fields)

    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200

    after = reader.read_package_model(SLUG)
    assert after.state == reader.STATE_LIVE
    assert after.model is not None
    # The fields the package declares, and only those.
    assert sorted(after.model.manifest.fields) == ["headline", "open_prs"]
    assert sorted(after.model.manifest.fields) != default_fields


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
    body["view"]["blocks"][0]["fields"] = [token_name]

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

    stored = parse_package(store.get(_page_slug(store)).content)
    assert sorted(stored["model"]["types"]) == ["headline", "open_prs"]


async def test_a_credential_shaped_block_id_is_refused_too(monkeypatch, store):
    """The other name set the page counts: a view block's id.

    Both reach a page as keys -- a field name through the read and the manifest, a
    block id through the frame's ``blocks`` mapping -- so a check catching only one
    would leave the same hole one key away.
    """
    from kiro_crew.platform import redact_via_context

    token_id = "ghp_" + "f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1"
    assert redact_via_context(token_id) != token_id, "not credential-shaped here"

    body = _package_body()
    body["view"]["blocks"][0]["id"] = token_id

    async with _client(monkeypatch) as client:
        resp = await _save(client, body)
        assert resp.status == 400, await resp.text()
        payload = await resp.json()

    assert payload["code"] == "unsafe_declared_name"
    assert "view block #" in payload["error"]
    assert token_id not in payload["error"]
    assert store.records == {}


async def test_the_real_store_round_trips_a_valid_package(monkeypatch, tmp_path):
    """The positive control for the test above, through the real store.

    Without it, a route that refused EVERY package would pass the surrogate test,
    so this is what proves the 400 came from the surrogate and not from the save
    path being broken against a real store.
    """
    from kiro_crew.artifacts import ArtifactStore

    real = ArtifactStore(tmp_path / "artifacts")
    monkeypatch.setattr(routes, "_dashboard_artifact_store", lambda: real, raising=False)
    async with _client(monkeypatch) as client:
        resp = await _save(client, _package_body())
        assert resp.status == 200, await resp.text()
        assert "saved" in await resp.json()
        # A second save of the same layout, through the real store's own
        # fingerprint comparison rather than the stand-in's.
        again = await _save(client, _package_body())
        assert again.status == 200
        assert (await again.json())["saved"]["versioned"] is False

    stored = parse_package(real.get(_page_slug(real)).content)
    assert stored["bound_to"] == f"crewmate:{SLUG}"
    assert sorted(stored["model"]["types"]) == ["headline", "open_prs"]
    assert real.get(_page_slug(real)).version == 1


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


async def test_a_page_bound_to_another_crewmate_is_never_written_over(monkeypatch, store):
    """The authorization test. Drop the caller check and this one goes red.

    The other crewmate's page is already in the store, and the saving caller
    resolves to ``fleet-crew``. A handler that located the page by anything other
    than its own vetted binding -- the newest dashboard artifact, the only one, a
    slug it derived from a body field -- would update this record.
    """
    theirs = _package_body()
    theirs["kind"] = DASHBOARD_KIND
    theirs["bound_to"] = f"crewmate:{OTHER_SLUG}"
    foreign = store.create(
        name="other-crew dashboard", content=json.dumps(theirs), kind=DASHBOARD_KIND
    )
    before = foreign.content

    async with _client(monkeypatch) as client:
        mine = _package_body()
        mine["theme"]["tokens"]["--accent"] = "oklch(0.2 0.01 0)"
        resp = await _save(client, mine)
        assert resp.status == 200
        assert "saved" in await resp.json()

    assert _page_slug(store) != foreign.slug
    assert store.get(foreign.slug).content == before
    assert parse_package(store.get(foreign.slug).content)["bound_to"] == (f"crewmate:{OTHER_SLUG}")
    assert parse_package(store.get(_page_slug(store)).content)["bound_to"] == (f"crewmate:{SLUG}")


async def test_the_caller_updates_only_the_page_its_own_binding_names(monkeypatch, store):
    """Two pages in the store, and a second save reaches exactly one of them."""
    theirs = _package_body()
    theirs["kind"] = DASHBOARD_KIND
    theirs["bound_to"] = f"crewmate:{OTHER_SLUG}"
    foreign = store.create(
        name="other-crew dashboard", content=json.dumps(theirs), kind=DASHBOARD_KIND
    )

    async with _client(monkeypatch) as client:
        assert (await _save(client, _package_body())).status == 200
        changed = _package_body()
        changed["view"]["blocks"].append({"id": "extra", "type": "stat", "fields": ["open_prs"]})
        resp = await _save(client, changed)
        assert resp.status == 200
        assert "saved" in await resp.json()

    assert len(store.records) == 2
    mine = _page_slug(store)
    assert mine != foreign.slug
    # The first save CREATED this crewmate's page, so the one update recorded is
    # the second save -- and it names the caller's own slug, never the foreign one.
    assert [u["slug"] for u in store.updates] == [mine]
    assert len(parse_package(store.get(mine).content)["view"]["blocks"]) == 3
    assert len(parse_package(store.get(foreign.slug).content)["view"]["blocks"]) == 2


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
        return {"saved": {"slug": "d-1", "version": 2, "versioned": True, "blocks": 2}}

    monkeypatch.setattr(mcp_panel, "_post", _post)
    body = _package_body()
    body["bound_to"] = "crewmate:someone-else"
    out = mcp_panel._call_tool_inner("dashboard_save", body)
    assert sent["path"] == "/api/agent-panel/dashboard/save"
    assert set(sent["payload"]) == {"model", "view", "theme"}
    assert "version 2" in out


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
