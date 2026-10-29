"""Artifact folders and pins: the folder routes, folder icons, moving an artifact into
a folder, pinning it, and the publication withdrawal a folder delete runs.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from aiohttp import web

from kiro_crew import publish_sync
from kiro_crew.artifacts import (
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactReplacedError,
    ArtifactValidationError,
    get_default_folder_store,
    get_default_store,
)
from kiro_crew.dashboard.body_numbers import body_int
from kiro_crew.dashboard.chat_folders import generate_emoji_for_name
from kiro_crew.dashboard.handlers._shared import _is_restricted_session
from kiro_crew.dashboard.handlers.artifacts_parts.record_serialization import (
    _redact_text,
    _serialize,
)
from kiro_crew.dashboard.handlers.artifacts_parts.route_plumbing import (
    _audit,
    _err,
    _json_response,
    _non_owner_refusal,
    _read_json_body,
    _resolve_folder_ref_off_loop,
    _run_off_loop,
    _set_folder_and_reload,
    logger,
)
from kiro_crew.executors import subprocess_executor


def _set_pinned_and_reload(slug: str, pinned: bool) -> Any:
    """Set an artifact's pin mark and return the reloaded record (blocking)."""
    store = get_default_store()
    store.set_pinned(slug, pinned)
    return store.get(slug)


# ── Folders ─────────────────────────────────────────────────────
def _serialize_folder(folder: dict[str, Any], *, path: str | None = None) -> dict[str, Any]:
    """Serialize a folder record; redact the (user/LLM-set) name, icon, and path."""
    out = dict(folder)
    if isinstance(out.get("name"), str) and out["name"]:
        out["name"] = _redact_text(out["name"])
    # icon is LLM-derived (generate_emoji_for_name) or user-supplied (set_icon
    # API) — never trust either on the way back out to the dashboard.
    if isinstance(out.get("icon"), str) and out["icon"]:
        out["icon"] = _redact_text(out["icon"])
    if path is not None:
        out["path"] = _redact_text(path) if path else path
    return out


async def api_artifact_folders(request: web.Request) -> web.Response:
    """GET /api/artifact-folders — list folders enriched with item_count + path."""
    store = get_default_store()
    fstore = get_default_folder_store()
    try:
        # list_with_counts walks every artifact's meta.json (O(N) filesystem
        # scan). Offload it so the dashboard event loop stays responsive —
        # same pattern as api_chat_folders. breadcrumb() rides along in the
        # same executor call: it takes the folder-store lock once per folder,
        # so leaving it inline would put O(folders) lock acquisitions back on
        # the loop and cost a round trip each.
        def _list_serialized() -> list[dict[str, Any]]:
            return [
                _serialize_folder(f, path=fstore.breadcrumb(f["id"]))
                for f in fstore.list_with_counts(store)
            ]

        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(subprocess_executor(), _list_serialized)
    except (ArtifactError, OSError) as exc:
        logger.warning("artifact folder list failed: %s", exc)
        return _err(str(exc), status=500)
    return _json_response({"folders": out})


def _spawn_artifact_folder_icon_task(
    request: web.Request, folder_id: str, name: str, *, expected_epoch: int
) -> None:
    """Fire-and-forget: derive a single-emoji icon for an artifact folder via
    the shared LLM helper (same mechanism as chat-sidebar folders) and store
    it. Best-effort — any failure leaves the folder with the default glyph.

    The write-back goes through ``set_icon_if_epoch``, which re-finds the
    folder, checks the epoch and writes inside ONE critical section under the
    store lock. So a folder deleted while generation was in flight is never
    resurrected, and a manual icon set, an icon clear, or a rename that lands
    while generation is pending wins over the stale generated result.
    ``expected_epoch`` is the epoch this generation is pinned to, and each
    caller obtains it without ever reading the epoch back: the rename path takes
    the value :meth:`ArtifactFolderStore.rename` returns from inside its own
    bump's critical section, and the create path pins 0, which a fresh folder's
    epoch is by construction. A read-back would be a second lock acquisition and
    could capture a competing mutation's epoch."""
    state = request.app.get("state")
    if state is None:
        return

    async def _run() -> None:
        try:
            icon = await generate_emoji_for_name(state, name)
            if not icon:
                return
            fstore = get_default_folder_store()
            # No exists() pre-check: it was a TOCTOU gap (the folder could go
            # away, or its icon change, between the check and the write) and
            # set_icon_if_epoch subsumes it by re-finding under the lock.
            await _run_off_loop(lambda: fstore.set_icon_if_epoch(folder_id, icon, expected_epoch))
        except Exception:  # noqa: BLE001 — best-effort background task
            logger.debug("artifact folder icon generation failed for %s", folder_id, exc_info=True)

    task = asyncio.ensure_future(_run())
    _ARTIFACT_FOLDER_ICON_TASKS.add(task)
    task.add_done_callback(_ARTIFACT_FOLDER_ICON_TASKS.discard)


# Keep strong refs so in-flight icon tasks aren't garbage-collected mid-run.
_ARTIFACT_FOLDER_ICON_TASKS: set[asyncio.Task[None]] = set()


async def api_artifact_folder_create(request: web.Request) -> web.Response:
    """POST /api/artifact-folders — create a folder. Body: {name, parent?|parent_id?}."""
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_folder_create")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_folder_create",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
        )
        return _err("restricted session cannot create folders", status=403)
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    name = str(body.get("name") or "").strip()
    if not name:
        return _err("name required")
    # ``parent`` accepts an id OR a human path (mkdir -p); ``parent_id`` is
    # id-only — resolved read-only so a path-looking value can never
    # auto-create folders through the id-only key.
    if "parent" in body:
        parent_id, ferr = await _resolve_folder_ref_off_loop(
            body.get("parent"), create_missing=True
        )
    else:
        parent_id, ferr = await _resolve_folder_ref_off_loop(
            body.get("parent_id"), create_missing=False
        )
    if ferr:
        _audit(tool="artifact_folder_create", request=request, outcome="denied", error=ferr)
        return _err(ferr)
    fstore = get_default_folder_store()
    color = str(body.get("color") or "")
    try:
        folder = await _run_off_loop(lambda: fstore.create(name, parent_id=parent_id, color=color))
    except ArtifactValidationError as exc:
        _audit(tool="artifact_folder_create", request=request, outcome="denied", error=str(exc))
        return _err(str(exc))
    except ArtifactError as exc:
        _audit(tool="artifact_folder_create", request=request, outcome="error", error=str(exc))
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_folder_create",
        request=request,
        outcome="success",
        extra={"folder_id": folder["id"]},
    )
    path = await _run_off_loop(lambda: fstore.breadcrumb(folder["id"]))
    # Derive an emoji icon from the name in the background (chat-folder parity).
    # A just-created folder's icon epoch is 0 by construction -- create() never
    # touches the registry, and delete() pops its entry, so even a reused id
    # reads 0. Passing the literal is deliberate rather than re-reading it: a
    # read here would be a second lock acquisition, and any mutation landing in
    # that window would raise the epoch, so the read would capture the
    # COMPETING value and the generated icon would then clobber it. With 0
    # pinned, any such mutation makes the write-back lose, which is correct.
    # Spawned AFTER the last await so nothing yields to the loop between the
    # spawn and the response -- the task starts once the caller resumes.
    _spawn_artifact_folder_icon_task(request, folder["id"], name, expected_epoch=0)
    return _json_response(_serialize_folder(folder, path=path), status=201)


async def api_artifact_folder_update(request: web.Request) -> web.Response:
    """PATCH /api/artifact-folders/{id} — rename / reparent / reorder / icon."""
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_folder_update")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_folder_update",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"folder_id": request.match_info.get("id", "")},
        )
        return _err("restricted session cannot update folders", status=403)
    fid = request.match_info.get("id", "")
    fstore = get_default_folder_store()
    if not await _run_off_loop(lambda: fstore.exists(fid)):
        return _err("folder not found", status=404)
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    folder = await _run_off_loop(lambda: fstore.get(fid))
    if folder is None:  # exists() checked above; guards against a concurrent delete
        return _err("folder not found", status=404)

    # Set by _apply_updates when it renames: the icon epoch the rename's own
    # bump produced, read inside that same critical section. The handler must
    # arm background generation with THIS value and never re-read the epoch
    # afterwards -- see ArtifactFolderStore.rename for why a re-read is a race.
    armed_icon_epoch: dict[str, int] = {}

    def _apply_updates() -> dict[str, Any]:
        # Each mutation persists via _save() (fsync/replace) — runs in the
        # executor, off the event loop.
        f = fstore.get(fid)
        if f is None:
            raise ArtifactNotFoundError(f"folder not found: {fid}")
        if "name" in body:
            f, armed_icon_epoch["value"] = fstore.rename(fid, str(body["name"]))
        if "parent_id" in body:
            f = fstore.reparent(fid, str(body.get("parent_id") or ""))
        if "icon" in body:
            f = fstore.set_icon(fid, str(body.get("icon") or ""))
        if "color" in body:
            f = fstore.set_color(fid, str(body.get("color") or ""))
        if "order" in body:
            fstore.reorder([{"id": fid, "order": body_int(body["order"])}])
            ref = fstore.get(fid)
            if ref is not None:
                f = ref
        return f

    try:
        updated = await _run_off_loop(_apply_updates)
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except (ArtifactValidationError, ValueError, TypeError) as exc:
        _audit(
            tool="artifact_folder_update",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"folder_id": fid},
        )
        return _err(str(exc))
    except ArtifactError as exc:
        _audit(
            tool="artifact_folder_update",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"folder_id": fid},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_folder_update", request=request, outcome="success", extra={"folder_id": fid}
    )
    # A rename re-derives the emoji icon from the new name (chat-folder
    # parity) — unless this same request set an explicit icon, which wins.
    if "name" in body and "icon" not in body:
        # Arm with the epoch the rename itself produced, captured under the
        # rename's own lock. Re-reading it here instead would pick up a manual
        # icon set that landed in between and let the generated icon overwrite
        # it. A missing value means the rename did not run, so nothing to arm.
        epoch = armed_icon_epoch.get("value")
        if epoch is not None:
            _spawn_artifact_folder_icon_task(request, fid, str(body["name"]), expected_epoch=epoch)
    path = await _run_off_loop(lambda: fstore.breadcrumb(fid))
    return _json_response(_serialize_folder(updated, path=path))


async def _withdraw_subtree_publications(folder_id: str, fstore: Any) -> tuple[str, dict[str, str]]:
    """Withdraw every published copy in a folder subtree BEFORE it is cascaded away.

    Returns ``("", generations)`` when every copy is withdrawn (or there were none),
    otherwise ``(slug, generations)`` naming the FIRST artifact whose copy could not be
    withdrawn -- the caller then refuses the whole cascade and destroys nothing.

    ``generations`` maps every artifact this found in the subtree, withdrawn or not, to its
    ``created_at``, and the caller needs it for a second reason: it holds each one's
    publication guard across the destroy, so a first publish cannot be halfway through on
    an artifact about to be destroyed. Returning the map rather than letting the caller
    re-derive it keeps the two from disagreeing, and keeps the listing to one pass over the
    store.

    The stamp travels with each slug because a slug is only a name. This pass awaits a
    network withdrawal per copy, and a freed slug is re-minted identically, so an artifact
    listed here can be deleted and a same-titled newcomer can take its name before the
    destroy reads the map. The stamp is what lets the store tell them apart, and here too:
    an artifact whose stamp has already moved on is not the one this pass listed, so it is
    left entirely alone -- not withdrawn, and not cleared.

    Stops at the first failure rather than collecting them all: the question the caller is
    asking is "may I destroy this subtree", and one un-withdrawable copy settles it. A copy
    withdrawn before that point is genuinely gone, so its record is cleared -- it would
    otherwise keep naming a destination that does not hold it.
    """
    ids = await _run_off_loop(lambda: fstore.subtree_ids(folder_id))
    if not ids:
        return "", {}
    store = get_default_store()
    generations = await _run_off_loop(
        lambda: {
            a.slug: a.created_at for a in store.list() if (getattr(a, "folder_id", "") or "") in ids
        }
    )
    for slug, born in generations.items():
        try:
            art = await _run_off_loop(lambda s=slug: store.get(s))
        except ArtifactError:  # pragma: no cover -- listed, then vanished
            continue
        if art.created_at != born:
            # Replaced between the listing and here, so this is not the artifact this pass
            # is responsible for. Withdrawing or clearing would act on a newcomer nobody
            # named; the destroy re-checks the same stamp and leaves it alone too.
            continue
        if art.publication is None:
            continue
        outcome = await publish_sync.delete_for_artifact(art)
        if outcome in (
            publish_sync.DeleteWithdrawal.FAILED,
            publish_sync.DeleteWithdrawal.UNREACHABLE,
        ):
            return slug, generations
        # Withdrawn, or nothing was there: the destination does not serve this copy, so
        # the record must stop claiming it. The cascade deletes the artifact moments later;
        # clearing here keeps the state honest if the cascade itself then fails. Pinned to
        # the generation AND the record whose copy came down, because the withdrawal above
        # is a network wait: a newcomer holding this slug by now owns a record this pass
        # never withdrew, and so does this same artifact re-published in that window, which
        # leaves `created_at` unchanged. Either record is the only handle able to take ITS
        # copy down.
        withdrawn_id = art.publication.artifact_id
        try:
            await _run_off_loop(
                lambda s=slug, b=born, p=withdrawn_id: store.clear_publication(
                    s, expect_created_at=b, expect_publication_id=p
                )
            )
        except ArtifactReplacedError:
            logger.warning(
                "withdrew the published copy of %s but left the record alone: the "
                "record under that slug names a different artifact or a different "
                "copy, so it belongs to a publication this pass never withdrew",
                slug,
            )
    return "", generations


async def api_artifact_folder_delete(request: web.Request) -> web.Response:
    """DELETE /api/artifact-folders/{id}?delete_contents=<bool>.

    Default (``delete_contents`` falsy) is the SAFE path: re-parent this
    folder's direct children (folders + artifacts) up to its parent and delete
    only this folder. ``delete_contents=true`` cascades the whole subtree,
    permanently deleting every descendant artifact.
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_folder_delete")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_folder_delete",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"folder_id": request.match_info.get("id", "")},
        )
        return _err("restricted session cannot delete folders", status=403)
    fid = request.match_info.get("id", "")
    fstore = get_default_folder_store()
    if not await _run_off_loop(lambda: fstore.exists(fid)):
        return _err("folder not found", status=404)
    raw = (request.query.get("delete_contents") or "").strip().lower()
    delete_contents = raw in ("1", "true", "yes")
    # The artifacts whose publication guard the destroy below holds, each mapped to the
    # ``created_at`` it had when the withdrawal pass listed it -- the guard holds a name,
    # so the stamp is what says which artifact the name stood for. Empty on the safe path,
    # which destroys no artifact at all -- it re-parents the children and removes one
    # folder, so no publication record is at risk there.
    subtree_generations: dict[str, str] = {}
    if delete_contents:
        # The cascade destroys artifacts through `ArtifactStore.delete`, which knows
        # nothing about publications -- so before this fix a cascade over a folder holding
        # a published artifact erased the record while the public copy stayed served, with
        # nothing left able to withdraw it. Exactly the single-delete defect, reached by a
        # different door, and it has to obey the same rule.
        #
        # Withdraw first, destroy nothing until every published copy in the subtree is
        # withdrawn. On the first copy that will NOT come down the whole cascade is
        # refused: a folder that refuses to delete is recoverable, an orphaned public copy
        # is not. A withdrawal that DID succeed has its record cleared so the artifact
        # stops claiming a destination that does not hold it.
        blocked, subtree_generations = await _withdraw_subtree_publications(fid, fstore)
        if blocked:
            msg = (
                "This folder was not deleted: the published copy of "
                f"{blocked} could not be withdrawn, and deleting it would leave a public "
                "copy with nothing able to take it down. Restore access to its "
                "destination and try again. Unpublishing it first will not release it "
                "either -- that refuses for the same reason."
            )
            _audit(
                tool="artifact_folder_delete",
                request=request,
                outcome="error",
                error=msg,
                extra={"folder_id": fid, "delete_contents": True, "blocked_slug": blocked},
            )
            return _err(msg, status=502)
    try:
        # delete() scans every artifact (O(N)) and, in cascade mode, recursively
        # removes directories — offload off the event loop.
        #
        # Under the publication guard of every artifact the withdrawal pass found, for the
        # reason the single-artifact delete takes it: a first publish uploads its object
        # before it writes the record, so the store's own `refuse_if_published` re-read
        # inside the cascade cannot see a publish that is halfway through, and the artifact
        # is destroyed with its copy already served. Holding the guards here means each of
        # those artifacts is either not being published at all or already recorded, in
        # which case the cascade keeps it instead of destroying it.
        #
        # Acquired in slug order so two cascades over overlapping subtrees take them in the
        # same order and cannot each hold what the other waits for. A publish holds exactly
        # one of these at a time, so it can never be the second party in a cycle.
        #
        # An artifact filed INTO the subtree after that pass is not among them, so the
        # cascade is told to leave anything outside the guarded set alone rather than
        # destroy it: a publish in flight for it is invisible to the store's own record
        # check, and the withdrawal pass never saw it either. It survives, unfiled, and is
        # reported below.
        #
        # Each guarded slug carries the `created_at` of the artifact that HELD it when the
        # pass listed it, because the guard protects a name and the destroy has to name an
        # artifact. The guard keeps a publish from running under it, not a deletion
        # followed by a recreation -- creating an artifact takes no guard at all, and a
        # freed slug is re-minted identically. So the store re-checks the stamp under its
        # own lock and keeps a newcomer instead of destroying it.
        loop = asyncio.get_running_loop()
        async with AsyncExitStack() as guards:
            guarded = sorted(subtree_generations)
            for slug in guarded:
                await guards.enter_async_context(publish_sync.publication_guard(slug))
            summary = await loop.run_in_executor(
                subprocess_executor(),
                lambda: fstore.delete(
                    fid,
                    delete_contents=delete_contents,
                    artifact_store=get_default_store(),
                    destroyable_generations=dict(subtree_generations),
                ),
            )
    except ArtifactNotFoundError as exc:
        return _err(str(exc), status=404)
    except ArtifactError as exc:
        _audit(
            tool="artifact_folder_delete",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"folder_id": fid},
        )
        return _err(str(exc), status=500)
    kept = list(summary.get("kept_published_artifact_slugs", []))
    _audit(
        tool="artifact_folder_delete",
        request=request,
        outcome="success",
        extra={
            "folder_id": fid,
            "delete_contents": delete_contents,
            "deleted_artifacts": len(summary.get("deleted_artifact_slugs", [])),
            "kept_published_artifacts": len(kept),
            "unguarded_artifacts": len(summary.get("unguarded_artifact_slugs", [])),
            "replaced_artifacts": len(summary.get("replaced_artifact_slugs", [])),
        },
    )
    payload: dict[str, Any] = {"ok": True, **summary}
    if kept:
        # Filed into the subtree AFTER the withdrawal preflight ran, so their copies
        # were never withdrawn and the cascade refused to destroy them. Say so: a
        # silently surviving artifact is how someone concludes the delete worked.
        payload["notice"] = (
            "The folder was deleted, but "
            + ", ".join(kept)
            + (" is" if len(kept) == 1 else " are")
            + " still published and so were kept rather than destroyed -- deleting "
            "them would have left a public copy with nothing able to take it down. "
            "They are now unfiled. Unpublish them first, then delete them."
        )
    unguarded = list(summary.get("unguarded_artifact_slugs", []))
    if unguarded:
        # Moved into the subtree while the withdrawal pass was running, so nothing here
        # can rule out a publish in flight for them and the cascade left them alone.
        # Saying so is the point: a surviving artifact nobody mentions reads as a bug.
        joined = ", ".join(unguarded)
        notice = (
            f"The folder was deleted, but {joined} "
            + ("was" if len(unguarded) == 1 else "were")
            + " moved into it while the delete was running, so "
            + ("it was" if len(unguarded) == 1 else "they were")
            + " left alone rather than destroyed -- a copy being published at that "
            "moment could not be taken down afterwards. "
            + ("It is" if len(unguarded) == 1 else "They are")
            + " now unfiled. Delete "
            + ("it" if len(unguarded) == 1 else "them")
            + " again if that is what you meant."
        )
        payload["notice"] = f"{payload['notice']} {notice}" if payload.get("notice") else notice
    replaced = list(summary.get("replaced_artifact_slugs", []))
    if replaced:
        # The artifact named at this slug was deleted while the cascade ran and a new one
        # was created under the same title, which takes the freed slug back. Destroying
        # that newcomer has no undo, so it was kept -- and saying which slug it is matters
        # more here than anywhere else: the name is shared with the artifact the owner
        # DID mean to delete, so silence reads as "the delete worked".
        joined = ", ".join(replaced)
        notice = (
            f"The folder was deleted, but the artifact at {joined} "
            + ("is" if len(replaced) == 1 else "are")
            + " not the "
            + ("one" if len(replaced) == 1 else "ones")
            + " the delete started on -- "
            + ("it was" if len(replaced) == 1 else "they were")
            + " created under the same name while it ran, so "
            + ("it was" if len(replaced) == 1 else "they were")
            + " kept rather than destroyed. "
            + ("It is" if len(replaced) == 1 else "They are")
            + " now unfiled."
        )
        payload["notice"] = f"{payload['notice']} {notice}" if payload.get("notice") else notice
    return _json_response(payload)


async def api_artifact_set_folder(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/folder — move an artifact into a folder.

    Body accepts ``{folder}`` (id OR human path, mkdir -p) or ``{folder_id}``
    (id-only). ``""`` / ``"root"`` / null unfiles. Metadata-only — no version bump.
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_set_folder")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot move artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        return _err(str(exc))
    if "folder" in body:
        folder_id, ferr = await _resolve_folder_ref_off_loop(
            body.get("folder"), create_missing=True
        )
    else:
        folder_id, ferr = await _resolve_folder_ref_off_loop(
            body.get("folder_id"), create_missing=False
        )
    if ferr:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error=ferr,
            extra={"slug": slug},
        )
        return _err(ferr)
    # A non-empty id passed directly must reference a real folder.
    fstore = get_default_folder_store()
    if folder_id and not await _run_off_loop(lambda: fstore.exists(folder_id)):
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error="folder not found",
            extra={"slug": slug, "folder_id": folder_id},
        )
        return _err("folder not found", status=400)
    try:
        art = await _run_off_loop(lambda: _set_folder_and_reload(slug, folder_id))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    except ArtifactError as exc:
        _audit(
            tool="artifact_set_folder",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_set_folder",
        request=request,
        outcome="success",
        extra={"slug": slug, "folder_id": folder_id},
    )
    return _json_response(_serialize(art, include_content=True))


async def api_artifact_set_pinned(request: web.Request) -> web.Response:
    """PATCH /api/artifacts/{slug}/pin — set/clear an artifact's pin mark.

    Body: ``{"pinned": true|false}``. Metadata-only — no version bump.
    """
    owner_denied = await _non_owner_refusal(request, "artifacts.artifact_set_pinned")
    if owner_denied is not None:
        return owner_denied
    state = request.app.get("state")
    if state is None or _is_restricted_session(state, request):
        _audit(
            tool="artifact_set_pinned",
            request=request,
            outcome="denied",
            error="restricted session" if state is not None else "missing dashboard state",
            extra={"slug": request.match_info.get("slug", "")},
        )
        return _err("restricted session cannot pin artifacts", status=403)
    slug = request.match_info.get("slug", "")
    try:
        body = await _read_json_body(request)
    except ArtifactValidationError as exc:
        _audit(
            tool="artifact_set_pinned",
            request=request,
            outcome="denied",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc))
    raw_pinned = body.get("pinned")
    if not isinstance(raw_pinned, bool):
        _audit(
            tool="artifact_set_pinned",
            request=request,
            outcome="denied",
            error="'pinned' must be a boolean",
            extra={"slug": slug},
        )
        return _err("'pinned' must be a boolean (true or false)")
    pinned = raw_pinned
    try:
        art = await _run_off_loop(lambda: _set_pinned_and_reload(slug, pinned))
    except ArtifactNotFoundError as exc:
        _audit(
            tool="artifact_set_pinned",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=404)
    except ArtifactError as exc:
        _audit(
            tool="artifact_set_pinned",
            request=request,
            outcome="error",
            error=str(exc),
            extra={"slug": slug},
        )
        return _err(str(exc), status=500)
    _audit(
        tool="artifact_set_pinned",
        request=request,
        outcome="success",
        extra={"slug": slug, "pinned": pinned},
    )
    return _json_response(_serialize(art, include_content=True))
