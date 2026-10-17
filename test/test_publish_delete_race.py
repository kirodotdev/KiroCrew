"""A destroy landing inside a first publish's upload window.

A first publish uploads its object BEFORE the record naming it exists, so for the length of
that upload the store answers "not published" about content that is already served. Anything
that destroys an artifact on the strength of that answer erases the only handle able to
withdraw the copy, and no later action reaches it.

The two doors that destroy an artifact hold the artifact's publication guard across the
removal, which is what makes the store's own ``refuse_if_published`` re-read decisive. These
tests drive a real publish that parks mid-upload and send each door in while it is parked.

Two residuals are PINNED here rather than fixed, each asserting the answer that holds today:
the guard spans neither event loops nor processes, and the blank-shell settle is a third door
that does not take it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import artifacts as art_mod
from kiro_crew import publish_provider, publish_sync
from kiro_crew.artifacts import (
    ArtifactFolderStore,
    ArtifactNotFoundError,
    ArtifactPublication,
    ArtifactStore,
)
from kiro_crew.dashboard.handlers import artifacts as art_handlers
from kiro_crew.dashboard.handlers.artifacts import (
    api_artifact_delete,
    api_artifact_folder_delete,
)
from kiro_crew.publish_provider import PublishProvider, PublishResult, PushResult

# ── Harness ─────────────────────────────────────────────────────────────────


class ParkedUploadProvider(PublishProvider):
    """A provider whose ``publish`` parks with the object already uploaded.

    ``uploaded`` is set at the point the destination copy is public and the engine has not
    yet been handed the handle, which is exactly the window under test. ``may_return``
    releases it, so a test decides how long the window stays open.
    """

    name = publish_provider.DEFAULT_PROVIDER
    display_name = "Parked Upload Provider"
    install_hint = "the test provider is unavailable"

    def __init__(self) -> None:
        self.uploaded = asyncio.Event()
        self.may_return = asyncio.Event()
        self.withdrawn: list[str] = []

    def available(self) -> bool:
        return True

    def view_url_for(self, external_id: str) -> str:
        return f"https://destination.example/{external_id}"

    async def publish(
        self,
        *,
        file_path: str,
        content_type: str,
        title: str,
        summary: str,
        tags: list[str],
        visibility: str,
        shared_with: list[str],
    ) -> PublishResult:
        # Everything above this line is the upload. The object is public from here.
        self.uploaded.set()
        await self.may_return.wait()
        return PublishResult(
            external_id="uuid-window",
            view_url="https://destination.example/uuid-window",
            version_number=1,
            concurrency_token="sha-window",
        )

    async def push_version(
        self, *, external_id: str, file_path: str, expected_token: str
    ) -> PushResult:
        return PushResult(version_number=2, concurrency_token="sha-2")

    async def update_sharing(
        self, *, external_id: str, visibility: str, shared_with: list[str]
    ) -> None:
        return None

    async def unpublish(self, *, external_id: str) -> None:
        self.withdrawn.append(external_id)


@pytest.fixture
def stores(tmp_path: Path, monkeypatch):
    """Isolated artifact + folder stores wired into the module globals."""
    store = ArtifactStore(root=tmp_path / "artifacts")
    fstore = ArtifactFolderStore(path=tmp_path / "artifact_folders.json")
    monkeypatch.setattr(art_mod, "_default_store", store)
    monkeypatch.setattr(art_mod, "_default_folder_store", fstore)
    return store, fstore


@pytest.fixture
def provider():
    """Register the parked-upload provider under the default provider name."""
    prov = ParkedUploadProvider()
    publish_provider.reset_providers()
    saved = dict(publish_provider._FACTORIES)
    publish_provider.register_provider(prov.name, lambda: prov)
    publish_provider._INSTANCES[prov.name] = prov
    yield prov
    publish_provider._FACTORIES.clear()
    publish_provider._FACTORIES.update(saved)
    publish_provider.reset_providers()


@pytest.fixture
def patch_restricted(monkeypatch):
    def _stub(_state, req) -> bool:
        return req.app.get("_restricted_session", False)

    monkeypatch.setattr(art_handlers, "_is_restricted_session", _stub)


def _request(*, match: dict | None = None, query: dict | None = None) -> MagicMock:
    req = MagicMock()
    req.headers = {"X-Session-Key": "dashboard:test"}
    req.match_info = match or {}
    req.query = query or {}
    req.read = AsyncMock(return_value=b"")
    state = MagicMock()
    state.get_slot.return_value = None
    req.app = {"state": state, "_restricted_session": False}
    return req


def _body(resp) -> dict:
    return json.loads(resp.body)


async def _let_the_other_task_run() -> None:
    """Yield long enough for an unguarded destroy to finish while the upload is parked."""
    for _ in range(20):
        await asyncio.sleep(0.005)


def _surviving(store: ArtifactStore, slug: str):
    try:
        return store.get(slug)
    except ArtifactNotFoundError:
        return None


# ── The two doors ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_delete_inside_the_upload_window_keeps_the_artifact_and_its_handle(
    stores, provider, patch_restricted
) -> None:
    """A single-artifact delete sent while the copy is uploading must not destroy it.

    The store cannot help here on its own: at the moment the delete reads it there is no
    publication record to find, because the engine writes one only once the upload returns.
    So the delete either waits for the publish or runs before it, and the assertion is the
    user-visible outcome of that -- the artifact is still there and still names its copy.
    """
    store, _ = stores
    art = store.create(name="Doc", content="hello", kind="text")

    publishing = asyncio.create_task(publish_sync.publish(art.slug, visibility="PUBLIC"))
    await asyncio.wait_for(provider.uploaded.wait(), timeout=5)
    assert (
        _surviving(store, art.slug).publication is None
    ), "harness check: the window is the stretch where the copy is up and unrecorded"

    deleting = asyncio.create_task(api_artifact_delete(_request(match={"slug": art.slug})))
    await _let_the_other_task_run()
    provider.may_return.set()

    publish_result = await asyncio.gather(publishing, return_exceptions=True)
    resp = await deleting

    assert not isinstance(publish_result[0], BaseException), (
        "the publish lost its artifact mid-upload, so the uploaded copy has no handle: "
        f"{publish_result[0]!r}"
    )
    survivor = _surviving(store, art.slug)
    assert survivor is not None, (
        "the artifact was destroyed while its copy was uploading, leaving a public copy "
        "with nothing able to withdraw it"
    )
    assert survivor.publication is not None, "the uploaded copy must keep its record"
    assert survivor.publication.artifact_id == "uuid-window"
    assert resp.status == 409, "the delete must be refused and say so, not silently complete"
    assert provider.withdrawn == [], "nothing was withdrawn, so the copy must stay recorded"


@pytest.mark.asyncio
async def test_a_folder_cascade_inside_the_upload_window_keeps_the_artifact(
    stores, provider, patch_restricted
) -> None:
    """The cascade is the same defect through a different door and obeys the same rule.

    Its withdrawal pass reads the same empty record the single delete does, so the guard is
    what stops the destroy from running between the upload and the record. The cascade's own
    rule then applies: an artifact it may not destroy is KEPT and reported.
    """
    store, fstore = stores
    folder = fstore.create("F")
    art = store.create(name="Doc", content="hello", kind="text", folder_id=folder["id"])

    publishing = asyncio.create_task(publish_sync.publish(art.slug, visibility="PUBLIC"))
    await asyncio.wait_for(provider.uploaded.wait(), timeout=5)

    deleting = asyncio.create_task(
        api_artifact_folder_delete(
            _request(match={"id": folder["id"]}, query={"delete_contents": "true"})
        )
    )
    await _let_the_other_task_run()
    provider.may_return.set()

    publish_result = await asyncio.gather(publishing, return_exceptions=True)
    resp = await deleting

    assert not isinstance(publish_result[0], BaseException), (
        "the cascade destroyed the artifact mid-upload, so the uploaded copy has no handle: "
        f"{publish_result[0]!r}"
    )
    survivor = _surviving(store, art.slug)
    assert survivor is not None, (
        "the cascade destroyed an artifact whose copy was uploading, leaving a public copy "
        "with nothing able to withdraw it"
    )
    assert survivor.publication is not None, "the uploaded copy must keep its record"
    assert resp.status == 200
    assert (
        art.slug in _body(resp)["kept_published_artifact_slugs"]
    ), "an artifact the cascade may not destroy must be reported as kept"


@pytest.mark.asyncio
async def test_a_cascade_leaves_alone_an_artifact_that_arrives_while_it_withdraws(
    stores, provider, patch_restricted
) -> None:
    """An artifact moved into the subtree mid-pass must not be destroyed.

    The withdrawal pass is not instantaneous: it awaits a network withdrawal per published
    copy, and a move into the folder during that stretch takes no publication guard. Such
    an artifact is outside the guarded set, so its publication state cannot be established
    from inside the store either -- a first publish in flight has uploaded its object and
    written no record. The cascade therefore leaves it in place and says so.
    """
    store, fstore = stores
    folder = fstore.create("F")
    early = store.create(name="Early", content="x", kind="text", folder_id=folder["id"])
    store.set_publication(
        early.slug,
        ArtifactPublication(
            provider=publish_provider.DEFAULT_PROVIDER,
            artifact_id="uuid-early",
            view_url="https://destination.example/uuid-early",
            visibility="PUBLIC",
        ),
    )
    arriving = store.create(name="Arriving", content="hello", kind="text", folder_id="")

    publishing = asyncio.create_task(publish_sync.publish(arriving.slug, visibility="PUBLIC"))
    await asyncio.wait_for(provider.uploaded.wait(), timeout=5)

    async def _withdraw_and_let_one_in(art):
        # The window exactly: the pass has begun, this artifact was not in its listing,
        # and its upload is already done.
        store.set_folder(arriving.slug, folder["id"])
        return publish_sync.DeleteWithdrawal.WITHDRAWN

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("kiro_crew.publish_sync.delete_for_artifact", _withdraw_and_let_one_in)
        resp = await api_artifact_folder_delete(
            _request(match={"id": folder["id"]}, query={"delete_contents": "true"})
        )

    provider.may_return.set()
    publish_result = await asyncio.gather(publishing, return_exceptions=True)

    assert not isinstance(publish_result[0], BaseException), (
        "the cascade destroyed the arriving artifact mid-upload, so its copy has no "
        f"handle: {publish_result[0]!r}"
    )
    assert resp.status == 200
    body = _body(resp)
    assert (
        arriving.slug in body["unguarded_artifact_slugs"]
    ), "an artifact the cascade could not guard must be reported, not destroyed"
    survivor = _surviving(store, arriving.slug)
    assert survivor is not None, "the arriving artifact must survive the cascade"
    assert survivor.publication is not None, "its uploaded copy must keep its record"
    assert arriving.slug in (body.get("notice") or ""), "the notice must name what survived"
    assert (
        early.slug in body["deleted_artifact_slugs"]
    ), "the guarded, withdrawn artifact must still be destroyed"


@pytest.mark.asyncio
async def test_unpublish_leaves_a_republished_record_alone(stores, provider) -> None:
    """The artifact is the SAME one; only its publication changed under the withdrawal.

    This is the case the generation stamp cannot see: `set_publication` replaces the
    publication block and leaves `created_at` untouched, so an artifact re-published during
    the network round trip carries an unchanged stamp over a brand-new live copy. Clearing
    on the strength of the stamp erases that copy's only withdrawal handle. What settles it
    is the record's own id.
    """
    store, _ = stores
    art = store.create(name="Republished", content="x", kind="text")
    store.set_publication(
        art.slug,
        ArtifactPublication(
            provider=publish_provider.DEFAULT_PROVIDER,
            artifact_id="uuid-first",
            view_url="https://destination.example/uuid-first",
            visibility="PUBLIC",
        ),
    )

    async def _withdraw_then_republish(*, external_id: str) -> None:
        provider.withdrawn.append(external_id)
        store.set_publication(
            art.slug,
            ArtifactPublication(
                provider=publish_provider.DEFAULT_PROVIDER,
                artifact_id="uuid-second",
                view_url="https://destination.example/uuid-second",
                visibility="PUBLIC",
            ),
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(provider, "unpublish", _withdraw_then_republish)
        await publish_sync.unpublish(art.slug)

    assert provider.withdrawn == ["uuid-first"], "the asked-for copy must be withdrawn"
    survivor = store.get(art.slug)
    assert survivor.created_at == art.created_at, "this is the same artifact throughout"
    assert survivor.publication is not None, (
        "the unpublish cleared a record it never withdrew: the artifact was re-published "
        "during the round trip, and that copy's only handle is now gone"
    )
    assert survivor.publication.artifact_id == "uuid-second"


@pytest.mark.asyncio
async def test_unpublish_leaves_a_replacement_s_own_record_alone(stores, provider) -> None:
    """The withdrawal is a network round trip, and this path holds no guard at all.

    So the slug it clears afterwards can hold a DIFFERENT artifact by then: one created
    and published while the removal was in flight. Clearing on the strength of the name
    discards THAT artifact's record, which is the only handle able to withdraw the copy it
    is serving, and nothing later reaches that copy. The withdrawal this call asked for did
    complete, so the replacement is not an error -- its record is simply not this call's to
    discard.
    """
    store, _ = stores
    original = store.create(name="Shared", content="x", kind="text")
    store.set_publication(
        original.slug,
        ArtifactPublication(
            provider=publish_provider.DEFAULT_PROVIDER,
            artifact_id="uuid-original",
            view_url="https://destination.example/uuid-original",
            visibility="PUBLIC",
        ),
    )
    reborn: dict[str, str] = {}

    async def _withdraw_then_let_a_namesake_in(*, external_id: str) -> None:
        provider.withdrawn.append(external_id)
        store.delete(original.slug)
        replacement = store.create(name="Shared", content="mine", kind="text")
        assert replacement.slug == original.slug, "the store did not re-mint the freed slug"
        store.set_publication(
            replacement.slug,
            ArtifactPublication(
                provider=publish_provider.DEFAULT_PROVIDER,
                artifact_id="uuid-replacement",
                view_url="https://destination.example/uuid-replacement",
                visibility="PUBLIC",
            ),
        )
        reborn["created_at"] = replacement.created_at

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(provider, "unpublish", _withdraw_then_let_a_namesake_in)
        await publish_sync.unpublish(original.slug)

    assert provider.withdrawn == ["uuid-original"], "the asked-for copy must be withdrawn"
    survivor = _surviving(store, original.slug)
    assert survivor is not None
    assert survivor.created_at == reborn["created_at"], "the survivor must be the replacement"
    assert survivor.publication is not None, (
        "the unpublish cleared the replacement's own record, which is the only handle "
        "able to take ITS copy down"
    )
    assert survivor.publication.artifact_id == "uuid-replacement"


def test_every_destroy_door_removes_the_directory_off_the_loop() -> None:
    """The removal is a recursive directory walk, and both doors are `async def`.

    Run on the gateway's only event loop, that walk stalls chat and the liveness
    heartbeat, and the watchdog answers a stalled loop by killing and respawning the
    gateway. Holding the publication guard does not change that: the guard decides WHICH
    artifact may be removed, not what thread removes it. So each door is asserted to hand
    the call to a worker, with whitespace collapsed because the call wraps.
    """

    def collapsed(fn) -> str:
        return " ".join(inspect.getsource(fn).split())

    single = collapsed(art_handlers.api_artifact_delete)
    assert "_run_off_loop( lambda: get_default_store().delete(" in single, (
        "the single-artifact delete runs the recursive removal on the event loop; a "
        "versioned artifact on slow storage then stalls every session's turn"
    )

    cascade = collapsed(art_handlers.api_artifact_folder_delete)
    assert "run_in_executor(" in cascade and "fstore.delete(" in cascade, (
        "the cascade runs its subtree removal on the event loop, which is the same stall "
        "over more artifacts"
    )


def test_every_post_await_publication_clear_is_generation_pinned() -> None:
    """The clear is only safe when it names the artifact whose copy came down.

    `clear_publication` is reached after a network withdrawal on every path that calls it,
    and a slug is a name the store re-mints identically, so a caller that clears by slug
    alone can discard a newcomer's record. The store's own docstring states that every
    post-await caller passes the stamp; this enumerates the callers so a new one cannot be
    added without it, which is the failure this file exists to prevent -- one call site was
    written as a method REFERENCE rather than a call, so a search for `clear_publication(`
    did not list it.
    """
    import pathlib

    root = pathlib.Path(art_mod.__file__).parent
    callers: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for num, line in enumerate(lines, 1):
            if "clear_publication" not in line:
                continue
            if "def clear_publication" in line:
                continue  # the definition itself
            # A wrapped call carries its keyword on a following line, so judge the call,
            # not the line that happens to hold the name.
            window = " ".join(lines[num - 1 : num + 3])
            callers.append((f"{path.name}:{num}", window.strip()))

    assert len(callers) >= 3, (
        f"expected every clear_publication caller to be found, got {callers} -- a caller "
        "spelled as a method reference is easy to miss and is exactly what this asserts"
    )
    unpinned = [where for where, window in callers if "expect_created_at" not in window]
    assert not unpinned, (
        "these clear_publication call sites do not name the artifact generation they "
        f"clear, so each can discard a replacement's own handle: {unpinned}"
    )
    unidentified = [where for where, window in callers if "expect_publication_id" not in window]
    assert not unidentified, (
        "these clear_publication call sites do not name the PUBLICATION they withdrew, so "
        "a re-publish during the round trip passes the generation check unchanged and "
        f"loses a live copy's only handle: {unidentified}"
    )


@pytest.mark.asyncio
async def test_a_cascade_spares_a_replacement_that_took_a_named_slug(
    stores, patch_restricted
) -> None:
    """A slug the cascade named, holding a DIFFERENT artifact by the time it destroys.

    A slug is a name, not an identity: the store re-mints a freed slug identically, so an
    artifact created under the same title after the earlier one is gone answers to exactly
    the slug the cascade wrote down. The pass between writing it down and destroying it is
    a network withdrawal per copy, which is long enough for that to happen -- and the
    publication guard does not close it, because creating an artifact takes no guard.

    Both halves of the answer are asserted from one window. The replacement is not
    destroyed, because destroying it is unrecoverable and nobody asked for it. Its own
    publication record is not cleared either, because that record is the only handle able
    to withdraw ITS copy.
    """
    store, fstore = stores
    folder = fstore.create("F")
    doomed = store.create(name="Doomed", content="x", kind="text", folder_id=folder["id"])
    store.set_publication(
        doomed.slug,
        ArtifactPublication(
            provider=publish_provider.DEFAULT_PROVIDER,
            artifact_id="uuid-doomed",
            view_url="https://destination.example/uuid-doomed",
            visibility="PUBLIC",
        ),
    )
    reborn: dict[str, str] = {}

    async def _withdraw_then_let_a_namesake_in(art):
        # The window exactly: this copy is down, and before the cascade reaches the
        # removal the artifact is deleted and recreated under the same title.
        store.delete(art.slug)
        replacement = store.create(
            name="Doomed", content="mine", kind="text", folder_id=folder["id"]
        )
        assert replacement.slug == art.slug, "the store did not re-mint the freed slug"
        assert replacement.created_at != art.created_at, "the replacement needs its own stamp"
        store.set_publication(
            replacement.slug,
            ArtifactPublication(
                provider=publish_provider.DEFAULT_PROVIDER,
                artifact_id="uuid-replacement",
                view_url="https://destination.example/uuid-replacement",
                visibility="PUBLIC",
            ),
        )
        reborn["slug"] = replacement.slug
        reborn["created_at"] = replacement.created_at
        return publish_sync.DeleteWithdrawal.WITHDRAWN

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("kiro_crew.publish_sync.delete_for_artifact", _withdraw_then_let_a_namesake_in)
        resp = await api_artifact_folder_delete(
            _request(match={"id": folder["id"]}, query={"delete_contents": "true"})
        )

    assert resp.status == 200
    body = _body(resp)
    survivor = _surviving(store, doomed.slug)
    assert survivor is not None, (
        "the cascade destroyed the replacement: it matched the slug it named, and the "
        "removal it just did has no undo"
    )
    assert survivor.created_at == reborn["created_at"], "the survivor must be the replacement"
    assert survivor.publication is not None, (
        "the withdrawal pass cleared the replacement's own record, which is the only "
        "handle able to take ITS copy down"
    )
    assert survivor.publication.artifact_id == "uuid-replacement"
    assert doomed.slug in body["replaced_artifact_slugs"]
    assert doomed.slug not in body["deleted_artifact_slugs"]
    assert doomed.slug in (body.get("notice") or ""), "the notice must name what survived"


@pytest.mark.asyncio
async def test_an_absent_slug_delete_spares_an_artifact_created_in_its_window(
    stores, patch_restricted
) -> None:
    """A delete that read the slug as EMPTY must not destroy what appears there afterwards.

    Passing no generation means "I have no generation to compare", which performs no check
    at all -- so a create landing between the pre-read and the removal is destroyed, and the
    request reports an ordinary success for an artifact nobody asked to delete. Expected
    ABSENCE is therefore its own value, checked under the same lock that removes, and the
    newcomer is refused instead. Driven by creating the artifact from inside the guard the
    delete acquires, which is exactly that window.
    """
    store, _ = stores
    slug = "created-mid-delete"
    real = publish_sync.publication_guard

    @asynccontextmanager
    async def _create_it_inside_the_window(s: str):
        async with real(s):
            if s == slug and _surviving(store, slug) is None:
                store.create(name="Created Mid Delete", content="mine", kind="text")
            yield

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(publish_sync, "publication_guard", _create_it_inside_the_window)
        mp.setattr(art_handlers.publish_sync, "publication_guard", _create_it_inside_the_window)
        resp = await api_artifact_delete(_request(match={"slug": slug}))

    survivor = _surviving(store, slug)
    assert survivor is not None, (
        "the delete destroyed an artifact created inside its own window, and answered as "
        "though it had removed the one the caller asked about"
    )
    assert survivor.content is not None
    assert (
        resp.status == 409
    ), f"the newcomer must be refused, not reported as deleted: {resp.status}"


@pytest.mark.asyncio
async def test_the_guard_table_does_not_grow_with_request_volume(stores, patch_restricted) -> None:
    """A guard table keyed by request input must not be a table anyone can grow.

    The delete route guards any WELL-FORMED slug, including one the store cannot resolve:
    the artifact it has to exclude there is one created and first-published inside the
    delete's own window, which no pre-read can resolve. What makes that affordable is that
    the registry is refcounted and drops an entry once nobody holds or waits on it, so the
    table is bounded by concurrency rather than by traffic. Asserted over repeated deletes
    of distinct absent slugs, which is the shape an abuser would use.
    """
    store, _ = stores
    before = set(publish_sync._publish_locks)

    for n in range(5):
        resp = await api_artifact_delete(_request(match={"slug": f"no-such-artifact-{n}"}))
        assert resp.status == 404

    assert set(publish_sync._publish_locks) == before, (
        "absent-slug deletes left guard entries behind, so request volume grows the table: "
        f"{set(publish_sync._publish_locks) - before}"
    )
    assert publish_sync._publish_lock_users == {}, (
        "a guard's user count survived its request, so the entry can never be dropped: "
        f"{publish_sync._publish_lock_users}"
    )

    art = store.create(name="Real", content="x", kind="text")
    resp = await api_artifact_delete(_request(match={"slug": art.slug}))
    assert resp.status == 200
    assert set(publish_sync._publish_locks) == before, "a resolved delete leaked its entry"

    # The guard is still TAKEN for a well-formed slug -- dropping the entry afterwards is
    # not the same as never holding it, and only the second of those reopens the window.
    assert "publication_guard" in inspect.getsource(art_handlers.api_artifact_delete)
    assert "slug_is_well_formed" in inspect.getsource(art_handlers.api_artifact_delete), (
        "the delete route guards only slugs the store resolved again, so an artifact "
        "created inside its window is unguarded"
    )


def test_a_cascade_that_names_no_victims_destroys_nothing(stores) -> None:
    """The omitted argument means "I vouch for nothing", not "destroy everything".

    ``destroyable_generations`` carries the artifacts whose publication guard the caller
    holds, so the value that means "I hold none" has to be the one a caller reaches by
    forgetting the argument. Defaulting the other way puts the dangerous cascade one
    omission away and the safe one behind a keyword nobody is forced to type. Both halves
    are asserted from the same tree: omitted destroys neither artifact and names both, and
    the same call vouching for one destroys exactly that one.
    """
    store, fstore = stores
    omitted = fstore.create("Omitted")
    kept = store.create(name="kept", content="x", kind="text", folder_id=omitted["id"])
    also_kept = store.create(name="also", content="y", kind="text", folder_id=omitted["id"])

    summary = fstore.delete(omitted["id"], delete_contents=True, artifact_store=store)

    assert summary["deleted_artifact_slugs"] == [], (
        "a cascade with no named victims destroyed an artifact, so the default now means "
        "'destroy everything' and one forgotten keyword strands a published copy"
    )
    assert set(summary["unguarded_artifact_slugs"]) == {kept.slug, also_kept.slug}
    assert store.get(kept.slug) is not None
    assert store.get(also_kept.slug) is not None

    vouched = fstore.create("Vouched")
    spared = store.create(name="spared", content="x", kind="text", folder_id=vouched["id"])
    named = store.create(name="named", content="y", kind="text", folder_id=vouched["id"])

    summary = fstore.delete(
        vouched["id"],
        delete_contents=True,
        artifact_store=store,
        destroyable_generations={named.slug: named.created_at},
    )

    assert summary["deleted_artifact_slugs"] == [named.slug]
    assert set(summary["unguarded_artifact_slugs"]) == {spared.slug}
    assert store.get(spared.slug) is not None
    with pytest.raises(ArtifactNotFoundError):
        store.get(named.slug)


# ── Residuals, pinned at today's answer ──────────────────────────────────────


def test_the_publication_guard_spans_one_event_loop_only() -> None:
    """PIN: two live event loops are NOT excluded from each other by this guard.

    The guard binds per running loop, so two loops hold it at once -- proved by a barrier
    both threads must reach from INSIDE it. A separate process is excluded even less: it
    has its own lock table entirely. Both readings matter for the same reason: the guard
    covers the gateway, where the publish and the destroy paths run, and nothing wider.
    """
    both_inside = threading.Barrier(2, timeout=5)
    failures: list[BaseException] = []

    def _hold() -> None:
        async def _main() -> None:
            async with publish_sync.publication_guard("pinned-slug"):
                both_inside.wait()

        try:
            asyncio.run(_main())
        except BaseException as exc:  # noqa: BLE001 -- recorded, asserted below
            failures.append(exc)

    threads = [threading.Thread(target=_hold, name=f"guard-{i}") for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert failures == [], (
        "the barrier was not reached from inside the guard by both loops, so the guard now "
        "spans loops -- the pin is stale and this residual is closed: "
        f"{failures!r}"
    )


def test_every_door_that_destroys_an_artifact_is_accounted_for() -> None:
    """A per-door verdict, so a door is not left out by being forgotten.

    Two doors take the guard. The blank-shell settle is a third: it destroys a pristine
    shell on the same ``publication is None`` reading and does not take it, so a shell
    published and abandoned in the same breath can still strand its object. That object is
    an empty document rather than the owner's content, which is why it is pinned here with
    the answer that holds today rather than fixed alongside the other two.
    """
    guarded = {
        "api_artifact_delete": art_handlers.api_artifact_delete,
        "api_artifact_folder_delete": art_handlers.api_artifact_folder_delete,
    }
    for name, fn in guarded.items():
        assert "publication_guard" in inspect.getsource(
            fn
        ), f"{name} destroys an artifact without holding the publication guard"

    assert "publication_guard" not in inspect.getsource(art_handlers.api_artifact_settle_blank), (
        "the blank-shell settle now takes the guard -- the pin is stale and this residual "
        "is closed"
    )
