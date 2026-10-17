"""``POST /api/agents/detail/{name}/fork``, ``/publish`` and ``/reset``: a crew's private copy of a template, made, published as a shared template, or dropped."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        KAS_RESERVED_AGENT_IDS,
        OWNED_KIRO_AGENT_FILES,
        TEMPLATE_NAME_RE,
        CapabilityError,
        DashboardState,
        KiroCrewConfig,
        _AmbiguousTemplateName,
        _ForkBookkeepingFailed,
        _get_config_lock,
        _is_reserved_basename,
        _lineage_unverifiable_error,
        _load_template_specs,
        _PublishNameBound,
        _read_agent_spec,
        _rebind_crew_locked,
        _refresh_forked_templates,
        _require_owner,
        _reserved_binding_names,
        _spec_path_is_safe,
        _spec_stem_on_disk,
        _StaleBinding,
        _unlink_copy_unless_referenced,
        _UnverifiableLineage,
        _write_spec_file,
        agent_state,
        agents_spec_lock,
        clear_list_agents_cache,
        config_path,
        kiro_agents_dir_path,
        logger,
        read_config_text,
        update_config_locked,
    )


async def api_agent_fork(request: web.Request) -> web.Response:
    """POST /api/agents/detail/{name}/fork — give one crew a private copy of a template.

    Blueprint semantics: a crew's definition edits must not mutate the shared
    template file that other crews (and kiro-cli) read. The first edit forks a
    copy named after the crew, records lineage in the agent_state sidecar (the
    spec itself cannot carry it — kiro-cli rejects unknown fields and drops the
    whole agent), and rebinds the crew. All of it happens under the config lock
    so the agents sync loop can never observe the new file unbound and
    auto-create a ghost agent for it.
    """
    name = request.match_info["name"]
    denied = await _require_owner(request, "agent_detail.fork")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    crew = body.get("crew")
    if not isinstance(crew, str) or not crew.strip():
        return web.json_response({"error": "crew is required", "code": "crew_required"}, status=400)
    crew = crew.strip()

    state: DashboardState = request.app["state"]
    async with _get_config_lock():
        agents_dir = kiro_agents_dir_path()

        try:
            source, source_name, taken, source_path = await asyncio.to_thread(
                _load_template_specs, agents_dir, name, "api_agent_fork"
            )
        except _AmbiguousTemplateName:
            return web.json_response(
                {
                    "error": f"'{name}' matches more than one template file; rename one first.",
                    "code": "ambiguous_template_name",
                },
                status=409,
            )
        if source is None:
            return web.json_response(
                {"error": f"Template '{name}' not found", "code": "template_not_found"}, status=404
            )

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if crew not in cfg.agents:
            return web.json_response(
                {"error": f"Agent '{crew}' not found", "code": "agent_not_found"}, status=404
            )
        agent = cfg.agents[crew]
        # A stale or racing request must not clobber a newer binding: the fork
        # was issued against the crew's current template, so require it still is.
        if agent.kiro_agent not in (name, source_name):
            return web.json_response(
                {
                    "error": f"'{crew}' is no longer bound to '{source_name}'",
                    "code": "stale_binding",
                },
                status=409,
            )

        # Already this crew's own copy: nothing to fork. Idempotence keeps the
        # frontend's fork-before-first-edit call safe to repeat.
        fork = agent_state.get_fork_info(source_name)
        if fork and fork["private_to"] == crew:
            return web.json_response({"ok": True, "template": source_name, "already_private": True})

        # The copy is named after the crew — the fork is invisible, so there is
        # no naming step, and the crew's name is the one the user already knows.
        # Sanitized because crew names are free text and this becomes a filename
        # (a template's permanent identity; there is no rename). The declared
        # "name" is set equal to the stem below, which is what keeps discovery's
        # package-filename guess from misreading a dashed copy name.
        # Bounded to keep the filename (plus a collision suffix) inside the
        # 63-char template-name rule and every filesystem's component limit.
        base = re.sub(r"[^A-Za-z0-9_.-]+", "-", crew)[:48].strip("-.") or "agent"
        # The specs Kiro Crew itself generates (kirocrew.json, kirocrew-lite.json,
        # ...) are rebuilt on boot; a copy landing on one of those stems while
        # the managed file is absent would be overwritten by that rebuild, so
        # they count as taken whether or not the file exists right now. Same
        # rule the publish handler applies to a user-chosen name. The ids the
        # KAS engine keeps for itself (``KAS_RESERVED_AGENT_IDS``: ``default``,
        # the seeded first crewmate's own name, and the built-in mode ids) are
        # taken for the same reason, matched exactly as the engine matches
        # them: a copy on such a stem binds the crew to a mode KAS never
        # advertises, or to the engine's own agent instead of the copy.
        managed_stems = {Path(f).stem.lower() for f in OWNED_KIRO_AGENT_FILES}

        def _create_record_bind() -> tuple[str, Path]:
            """Create the file, record lineage, and rebind in ONE config-lock
            hold: released between those steps, a locked bind
            could land on the just-created file before its ownership existed,
            and the lineage would then record one owner while another crew is
            bound — every later edit silently hitting both. Spec lock inner,
            the same nesting every cleanup path uses; the sidecar writers
            never take the config lock, so the nesting cannot invert.

            `taken` is a pre-scan and can go stale; the in-lock exists() probe
            asks the filesystem with its own case semantics, and the exclusive
            create refuses whatever both still missed rather than truncating
            it. The SOURCE is re-read in-lock too: the pre-lock snapshot can
            miss a concurrent refresh's writes. Reserved Windows basenames
            (CON, NUL, …) are suffixed past like collisions. Every current
            binding is reserved as well — a crew bound to a MISSING name would
            otherwise capture the new copy, and the legacy
            global fallback `agent.default_agent` is a resolvable reference
            like any binding.
            """
            chosen: list[tuple[str, Path]] = []

            def _unwind(dest: Path, copy_name: str) -> None:
                # Locked writers cannot have bound the copy — we hold the
                # config lock — but a writer outside it (a hand-edited file,
                # a process that skips the sidecar lock) can, so the reference
                # check re-reads the FILE before unlinking.
                try:
                    raw = json.loads(read_config_text(config_path()))
                except Exception:
                    raw = {}
                if not isinstance(raw, dict):
                    raw = {}
                targets = {copy_name, dest.stem}
                bound = raw.get("agents")
                for entry in bound.values() if isinstance(bound, dict) else ():
                    if isinstance(entry, dict) and entry.get("kiro_agent") in targets:
                        logger.warning(
                            "a crew bound private copy %r mid-fork; leaving it in place",
                            copy_name,
                        )
                        with contextlib.suppress(Exception):
                            agent_state.prune(copy_name)
                        return
                with contextlib.suppress(OSError):
                    dest.unlink(missing_ok=True)
                with contextlib.suppress(Exception):
                    agent_state.prune(copy_name)

            # mypy checks this owner before the handlers module, defers this closure
            # and loses the enclosing function's narrowing of ``crew`` here.
            def _mutate(cfg_data: dict) -> dict:
                # Staleness FIRST: nothing is created for a bind that moved.
                entry = cfg_data.get("agents", {}).get(crew)
                if not isinstance(entry, dict) or entry.get("kiro_agent") not in (
                    name,
                    source_name,
                ):
                    raise _StaleBinding()
                bound = _reserved_binding_names(cfg_data)
                with agents_spec_lock(agents_dir):
                    if source_path is None:
                        raise FileNotFoundError(source_name)
                    fresh_source = _read_agent_spec(
                        source_path, operation="api_agent_fork", source="dashboard"
                    )
                    if fresh_source is None:
                        raise FileNotFoundError(source_path)
                    copy_name, suffix = base, 2
                    while (
                        copy_name.lower() in taken
                        or copy_name.lower() in bound
                        or copy_name.lower() in managed_stems
                        or copy_name in KAS_RESERVED_AGENT_IDS
                        or _is_reserved_basename(copy_name)
                        or _spec_stem_on_disk(agents_dir, copy_name)
                    ):
                        copy_name = f"{base}-{suffix}"
                        suffix += 1
                    data = dict(fresh_source)
                    data["name"] = copy_name
                    # Same rule as every other spec writer: bookkeeping keys
                    # never reach a kiro spec.
                    agent_state.lift_and_strip_bookkeeping(data, copy_name)
                    dest = agents_dir / f"{copy_name}.json"
                    _write_spec_file(dest, data)
                    # Lineage inside the SAME hold. The prune is NOT
                    # suppressed: a stale sidecar entry from a failed earlier
                    # delete must not ship on the new copy.
                    try:
                        agent_state.prune(copy_name)
                        agent_state.set_fork_info(
                            copy_name, forked_from=source_name, private_to=crew  # type: ignore[arg-type]
                        )
                        managed = agent_state.get_model_managed(source_name)
                        if managed is not None:
                            agent_state.set_model_managed(copy_name, managed)
                    except Exception:
                        logger.exception("fork bookkeeping failed for %r", copy_name)
                        _unwind(dest, copy_name)
                        raise _ForkBookkeepingFailed() from None
                    entry["kiro_agent"] = copy_name
                    chosen.append((copy_name, dest))
                return cfg_data

            update_config_locked(mutate=_mutate)
            return chosen[0]

        try:
            copy_name, dest = await asyncio.to_thread(_create_record_bind)
        except FileNotFoundError:
            return web.json_response(
                {
                    "error": f"Template '{source_name}' changed on disk; retry.",
                    "code": "source_changed",
                },
                status=409,
            )
        except _StaleBinding:
            return web.json_response(
                {
                    "error": f"'{crew}' is no longer bound to '{source_name}'",
                    "code": "stale_binding",
                },
                status=409,
            )
        except _ForkBookkeepingFailed:
            return web.json_response(
                {"error": "Could not record the copy's lineage", "code": "bookkeeping_failed"},
                status=500,
            )
        except Exception:
            logger.exception("fork failed for crew %r", crew)
            return web.json_response(
                {"error": "Could not create the copy", "code": "fork_failed"},
                status=500,
            )

    # A refresh pass can interleave between the lineage record and the rebind:
    # it then sees a fork with no corroborating binding, records it as failed,
    # and the spawn gate blocks the copy the user just forked. Re-running the
    # refresh AFTER the binding persisted re-corroborates it and rebuilds the
    # failure set. Outside the spec lock — the pass takes it
    # per fork. Best-effort for the RESPONSE only: the fork is committed
    # either way, and a refresh failure leaves the gate fail-closed (the
    # correct posture) rather than turning a committed fork into a 500.
    try:
        await asyncio.to_thread(_refresh_forked_templates)
    except Exception:
        logger.warning("post-fork governance refresh failed", exc_info=True)
    clear_list_agents_cache()
    state.push_refresh("agents")
    return web.json_response(
        {"ok": True, "template": copy_name, "filename": dest.name, "forked_from": source_name}
    )


async def api_agent_publish(request: web.Request) -> web.Response:
    """POST /api/agents/detail/{name}/publish — save a crew's private copy as a named template.

    The counterpart of the invisible fork: forking never asks for a name, so
    the one place a template name is ever chosen is here, deliberately, by the
    user. Publishes {name} (which must be *crew*'s private copy) under the
    caller-supplied new name with NO fork lineage — a real, shareable template —
    rebinds the crew to it, and removes the superseded private copy. A filename
    is a template's permanent identity (there is no rename), which is why the
    name is validated and collision-refused rather than suffixed.
    """
    name = request.match_info["name"]
    denied = await _require_owner(request, "agent_detail.publish")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )
    crew = body.get("crew")
    new_name = body.get("name")
    if not isinstance(crew, str) or not crew.strip():
        return web.json_response({"error": "crew is required", "code": "crew_required"}, status=400)
    if not isinstance(new_name, str) or not TEMPLATE_NAME_RE.fullmatch(new_name.strip()):
        return web.json_response(
            {
                "error": "name must be 1-63 letters, digits, dots, dashes or underscores",
                "code": "invalid_template_name",
            },
            status=400,
        )
    if _is_reserved_basename(new_name.strip()):
        # A filename Windows reserves at the filesystem level (CON, NUL, COM1…):
        # creating CON.json raises there, so the name can never be portable.
        return web.json_response(
            {
                "error": f"'{new_name.strip()}' is a reserved filename on Windows",
                "code": "invalid_template_name",
            },
            status=400,
        )
    crew = crew.strip()
    new_name = new_name.strip()
    if f"{new_name.lower()}.json" in {f.lower() for f in OWNED_KIRO_AGENT_FILES}:
        return web.json_response(
            {"error": f"'{new_name}' is reserved", "code": "template_name_reserved"}, status=400
        )
    if new_name in KAS_RESERVED_AGENT_IDS:
        # The KAS engine keeps its own agent under this id (or drops the entry)
        # without an error, so a template published under it never runs there.
        # Exact match, as the engine matches: ``Default`` registers normally.
        # Its own code, distinct from the runtime-owned stems above, so the
        # dashboard can say WHOSE name it is rather than "reserved" alone.
        return web.json_response(
            {
                "error": f"'{new_name}' is reserved by the KAS agent engine",
                "code": "template_name_reserved_by_engine",
            },
            status=400,
        )

    from kiro_crew.dashboard.handlers.agent_capabilities import inherited_template_action

    inherited = await inherited_template_action(request, crew, "publish", new_name)
    if inherited is not None:
        return inherited

    state: DashboardState = request.app["state"]
    async with _get_config_lock():
        agents_dir = kiro_agents_dir_path()

        try:
            source, source_name, taken, source_path = await asyncio.to_thread(
                _load_template_specs, agents_dir, name, "api_agent_publish"
            )
        except _AmbiguousTemplateName:
            return web.json_response(
                {
                    "error": f"'{name}' matches more than one template file; rename one first.",
                    "code": "ambiguous_template_name",
                },
                status=409,
            )
        if source is None:
            return web.json_response(
                {"error": f"Template '{name}' not found", "code": "template_not_found"}, status=404
            )
        # Case-folded: 'Reviewer' and 'reviewer' are the same file on the
        # case-insensitive filesystems macOS and Windows default to.
        if new_name.lower() in taken:
            return web.json_response(
                {"error": f"A template named '{new_name}' already exists", "code": "name_taken"},
                status=409,
            )

        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if crew not in cfg.agents:
            return web.json_response(
                {"error": f"Agent '{crew}' not found", "code": "agent_not_found"}, status=404
            )
        # Only a private copy can be published: publishing a template that is
        # already shared would silently duplicate it, and publishing another
        # crew's copy would leak their customization.
        fork = agent_state.get_fork_info(source_name)
        if not fork or fork["private_to"] != crew:
            return web.json_response(
                {
                    "error": f"'{source_name}' is not {crew}'s private copy",
                    "code": "not_a_private_copy",
                },
                status=409,
            )
        # A stale publish must not rebind over a newer binding (same guard as
        # fork). Fast pre-check only; the authoritative check re-runs inside
        # _rebind_crew_locked's critical section.
        if cfg.agents[crew].kiro_agent not in (name, source_name):
            return web.json_response(
                {
                    "error": f"'{crew}' is no longer bound to '{source_name}'",
                    "code": "stale_binding",
                },
                status=409,
            )

        def _create_published() -> Path:
            """Create under the spec lock; the exclusive write refuses any
            destination the pre-scan missed instead of truncating it. The
            source is re-read in-lock: the pre-lock snapshot can miss a
            concurrent refresh's writes and publish stale content.

            Runs under the config lock (outer) so a name some crew's binding
            references — with no file behind it — is refused before the file
            exists: the moment it does, that dangling binding resolves to it
            and the crew silently executes the published content (GPT
            round-52). Publish never suffixes, so this rejects. The
            publishing crew's own binding cannot hit: it points at the source,
            whose file exists and is caught by the name_taken pre-check.
            """
            created: list[Path] = []

            # mypy checks this owner before the handlers module, defers this closure
            # and loses the enclosing function's narrowing of ``crew`` and ``new_name`` here.
            def _check_bindings_then_create(cfg_data: dict) -> None:
                # Includes the legacy global fallback agent.default_agent —
                # a resolvable reference like any crew binding.
                if new_name.lower() in _reserved_binding_names(cfg_data):  # type: ignore[union-attr]
                    raise _PublishNameBound()
                with agents_spec_lock(agents_dir):
                    if source_path is None:
                        raise FileNotFoundError(source_name)
                    fresh_source = _read_agent_spec(
                        source_path, operation="api_agent_publish", source="dashboard"
                    )
                    if fresh_source is None:
                        raise FileNotFoundError(source_path)
                    data = dict(fresh_source)
                    data["name"] = new_name
                    dest = agents_dir / f"{new_name}.json"
                    if _spec_stem_on_disk(agents_dir, new_name):  # type: ignore[arg-type]
                        raise FileExistsError(dest)
                    # Lineage BEFORE the file exists: from its first byte on
                    # disk the destination is this crew's private copy, so no
                    # failure path between here and the rebind — a rollback
                    # whose unlink AND private-marking both fail included —
                    # can leave it discoverable as a shared template. The
                    # clean-slate prune runs first so a dead entry under this
                    # name (stale lineage, stale model state) is not inherited;
                    # its failure aborts the publish before anything is
                    # written. Success flips the copy to shared
                    # (``clear_fork_info``) once the crew is bound to it.
                    agent_state.prune(new_name)  # type: ignore[arg-type]
                    agent_state.set_fork_info(new_name, forked_from=source_name, private_to=crew)  # type: ignore[arg-type]
                    agent_state.lift_and_strip_bookkeeping(data, new_name)  # type: ignore[arg-type]
                    try:
                        _write_spec_file(dest, data)
                    except BaseException:
                        with contextlib.suppress(Exception):
                            agent_state.prune(new_name)  # type: ignore[arg-type]
                        raise
                    created.append(dest)
                # None: the binding read mutates nothing — the lock is held so
                # no binding write can land between the check and the create.
                return None

            update_config_locked(mutate=_check_bindings_then_create)
            return created[0]

        try:
            dest = await asyncio.to_thread(_create_published)
        except _PublishNameBound:
            return web.json_response(
                {
                    "error": f"A crew is bound to the name '{new_name}'",
                    "code": "name_bound",
                },
                status=409,
            )
        except FileExistsError:
            # A concurrent creator won the name between our pre-scan and the
            # exclusive create. Publish never suffixes: the name is the user's.
            return web.json_response(
                {"error": f"A template named '{new_name}' already exists", "code": "name_taken"},
                status=409,
            )
        except FileNotFoundError:
            return web.json_response(
                {
                    "error": f"Template '{source_name}' changed on disk; retry.",
                    "code": "source_changed",
                },
                status=409,
            )
        except Exception:
            # The sidecar prune / lineage write failed before the destination
            # existed: nothing to undo, the private copy and binding are intact.
            logger.exception("publish bookkeeping failed for %r", new_name)
            return web.json_response(
                {"error": "Could not record the template's lineage", "code": "bookkeeping_failed"},
                status=500,
            )

        # Undo for every post-create failure (bookkeeping OR rebind): locked.
        # Unlink FIRST; when the destination cannot be removed (locked file),
        # it stays what the create step already made it — this crew's private
        # copy — instead of surfacing as a shared template.
        # mypy checks this owner before the handlers module, defers this closure
        # and loses the enclosing function's narrowing of ``crew`` and ``new_name`` here.
        def _undo_publish() -> None:
            # Reference-aware, same helper as the superseded-copy cleanup:
            # another crew can bind the just-created destination before this
            # rollback runs, and unlinking it then breaks that crew's sessions
            # with "Mode not found". The check and the unlink are one critical
            # section under the config lock, so a binding writer cannot land
            # between them. A RETAINED destination already carries the
            # private lineage the create step wrote, so even a failing
            # re-assert here leaves nothing discoverable as shared.
            outcome = _unlink_copy_unless_referenced(dest, agents_dir, new_name, dest.stem)  # type: ignore[arg-type]
            if outcome == "deleted":
                with contextlib.suppress(Exception):
                    agent_state.prune(new_name)  # type: ignore[arg-type]
                return
            if outcome == "referenced":
                # Another crew adopted the destination before the rollback:
                # it is in live use as a shared template, so it stays one —
                # deleting it or leaving it marked private would break or
                # misattribute that crew's binding.
                with contextlib.suppress(Exception):
                    agent_state.clear_fork_info(new_name)  # type: ignore[arg-type]
                return
            logger.warning("rollback could not remove %r; it stays private to %r", new_name, crew)
            with contextlib.suppress(Exception):
                agent_state.set_fork_info(new_name, forked_from=source_name, private_to=crew)  # type: ignore[arg-type]

        # Offloaded: the sidecar mutators take a blocking cross-process
        # file_lock(wait=True), which must never run on the event loop.
        # mypy checks this owner before the handlers module, defers this closure
        # and loses the enclosing function's narrowing of ``new_name`` here.
        def _record_publish_model() -> None:
            # The clean-slate prune already ran in the create step, before
            # the file existed; only the source's model tracking is copied.
            managed = agent_state.get_model_managed(source_name)
            if managed is not None:
                agent_state.set_model_managed(new_name, managed)  # type: ignore[arg-type]

        try:
            await asyncio.to_thread(_record_publish_model)
        except Exception:
            logger.exception("publish bookkeeping failed for %r", new_name)
            await asyncio.to_thread(_undo_publish)
            return web.json_response(
                {"error": "Could not record the template's model", "code": "bookkeeping_failed"},
                status=500,
            )

        try:
            await asyncio.to_thread(_rebind_crew_locked, crew, (name, source_name), new_name)
        except CapabilityError as exc:
            await asyncio.to_thread(_undo_publish)
            return web.json_response({"error": exc.code, "code": exc.code}, status=exc.status)
        except _StaleBinding:
            await asyncio.to_thread(_undo_publish)
            return web.json_response(
                {
                    "error": f"'{crew}' is no longer bound to '{source_name}'",
                    "code": "stale_binding",
                },
                status=409,
            )
        except _UnverifiableLineage as exc:
            await asyncio.to_thread(_undo_publish)
            return web.json_response(
                {
                    "error": _lineage_unverifiable_error(new_name, exc),
                    "code": "lineage_unverifiable",
                },
                status=409,
            )
        except Exception:
            logger.exception("publish rebind failed for crew %r", crew)
            await asyncio.to_thread(_undo_publish)
            return web.json_response(
                {"error": "Could not update the crew's binding", "code": "rebind_failed"},
                status=500,
            )

        # The binding has moved: the destination is committed, so drop the
        # private lineage the create step wrote and let it list as the shared
        # template the user published. Nothing is exposed if this fails — the
        # crew is bound to what is then still its own private copy under the
        # new name — and the publish is COMMITTED (rebind persisted, file
        # created), so this must not become an HTTP error: the client keys its
        # editor off the response's template name, and an error would leave it
        # targeting the superseded copy while the crew runs the new one, so its
        # later edits would land on an inactive file. Reported as a warning on
        # the committed result instead; the stale lineage row is reconciled by
        # the next refresh sweep, like the prune below.
        publish_warning: str | None = None
        try:
            await asyncio.to_thread(agent_state.clear_fork_info, new_name)
        except Exception:
            logger.exception("publish could not mark %r shared", new_name)
            publish_warning = "publish_incomplete"

        def _cleanup_superseded() -> None:
            """Remove the superseded private copy — file first, lineage after.

            Under the spec lock like every other spec mutation; lineage
            outlives a failed delete, since pruning it while the file remains
            would surface the private customization as a shared template.
            Unlinks the RESOLVED ``source_path`` — a fork whose file stem
            differs from its declared name would be missed by
            ``{source_name}.json``, leaving the file while its lineage is
            pruned (the exact surface-as-shared failure above).
            """
            if source_path is None or not _spec_path_is_safe(source_path, agents_dir):
                return
            # This crew was rebound to the published name above, so any binding
            # still naming the copy is another crew's (pre-dating the bind-time
            # guard): the locked helper keeps file AND lineage in that case,
            # atomically against concurrent binding writes.
            # Stem included: a stem binding resolves the same file.
            if (
                _unlink_copy_unless_referenced(
                    source_path, agents_dir, source_name, source_path.stem
                )
                != "deleted"
            ):
                return
            # Non-throwing: the publish is already committed (rebind persisted,
            # file created). A sidecar failure here must not turn a committed
            # publish into an HTTP 500 whose retry then 404s; the stale lineage
            # row is reconciled by the next refresh sweep.
            with contextlib.suppress(Exception):
                agent_state.prune(source_name)

        await asyncio.to_thread(_cleanup_superseded)
    clear_list_agents_cache()
    state.push_refresh("agents")
    result: dict[str, object] = {"ok": True, "template": new_name, "filename": dest.name}
    if publish_warning:
        result["warning"] = publish_warning
    return web.json_response(result)


async def api_agent_reset(request: web.Request) -> web.Response:
    """POST /api/agents/detail/{name}/reset — discard a crew's private copy.

    A server-side transaction replacing the panel's client-orchestrated
    rebind-then-delete: the client sequence could rebind to an origin deleted
    after panel load and then delete the copy, leaving the crew bound to
    nothing. Here the origin's existence is validated, the rebind runs under
    the config lock with a stale-binding check, and only a PERSISTED rebind
    is followed by the locked delete of the copy.
    """
    denied = await _require_owner(request, "agent_reset")
    if denied is not None:
        return denied
    name = request.match_info["name"]
    try:
        body = await request.json()
    except ValueError:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    crew = body.get("crew") if isinstance(body, dict) else None
    if not isinstance(crew, str) or not crew:
        return web.json_response({"error": "crew is required", "code": "crew_required"}, status=400)

    from kiro_crew.dashboard.handlers.agent_capabilities import inherited_template_action

    inherited = await inherited_template_action(request, crew, "reset")
    if inherited is not None:
        return inherited

    state: DashboardState = request.app["state"]
    async with _get_config_lock():
        agents_dir = kiro_agents_dir_path()
        fork = agent_state.get_fork_info(name)
        if not fork or fork["private_to"] != crew:
            return web.json_response(
                {"error": f"'{name}' is not {crew}'s private copy", "code": "not_a_private_copy"},
                status=409,
            )
        origin = fork["forked_from"]
        try:
            origin_spec, origin_name, _taken, _path = await asyncio.to_thread(
                _load_template_specs, agents_dir, origin, "api_agent_reset"
            )
        except _AmbiguousTemplateName:
            return web.json_response(
                {
                    "error": f"'{origin}' matches more than one template file; rename one first.",
                    "code": "ambiguous_template_name",
                },
                status=409,
            )
        if origin_spec is None:
            # The origin vanished since the fork: rebinding would leave the
            # crew pointing at nothing, so the copy is KEPT and the client is
            # told why — the exact failure the client-side sequence shipped.
            return web.json_response(
                {"error": f"Origin template '{origin}' no longer exists", "code": "origin_missing"},
                status=409,
            )
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        if crew not in cfg.agents:
            return web.json_response(
                {"error": f"Agent '{crew}' not found", "code": "agent_not_found"}, status=404
            )
        if cfg.agents[crew].kiro_agent != name:
            return web.json_response(
                {"error": f"'{crew}' is no longer bound to '{name}'", "code": "stale_binding"},
                status=409,
            )
        origin_path = _path
        try:
            await asyncio.to_thread(_rebind_crew_locked, crew, (name,), origin_name, origin_path)
        except CapabilityError as exc:
            return web.json_response({"error": exc.code, "code": exc.code}, status=exc.status)
        except FileNotFoundError:
            # The origin vanished between validation and the rebind's critical
            # section (a cross-process delete): rebinding would leave the crew
            # pointing at nothing while the copy below gets deleted, so the
            # whole reset refuses instead.
            return web.json_response(
                {"error": f"Origin template '{origin}' no longer exists", "code": "origin_missing"},
                status=409,
            )
        except _StaleBinding:
            return web.json_response(
                {"error": f"'{crew}' is no longer bound to '{name}'", "code": "stale_binding"},
                status=409,
            )
        except _UnverifiableLineage as exc:
            # Ownership of the origin cannot be verified: refuse with the copy
            # kept rather than rebinding onto an unverifiable target.
            return web.json_response(
                {
                    "error": _lineage_unverifiable_error(origin_name, exc),
                    "code": "lineage_unverifiable",
                },
                status=409,
            )
        except Exception:
            logger.exception("reset rebind failed for crew %r", crew)
            return web.json_response(
                {"error": "Could not update the crew's binding", "code": "rebind_failed"},
                status=500,
            )

        # Rebind persisted: the copy is now unreferenced. Delete is
        # best-effort — a failure leaves a hidden private copy, never a
        # broken binding — mirroring _cleanup_superseded's semantics.
        def _delete_copy() -> None:
            # Resolve the copy's ACTUAL file rather than reconstructing it
            # from the declared name: a stem/name divergence (however it
            # arose) would otherwise leave the customized file on disk while
            # its lineage is pruned — a private copy would then appear
            # shared. Prune only after the file is actually gone.
            try:
                _copy_spec, _copy_name, _copy_taken, copy_path = _load_template_specs(
                    agents_dir, name, "api_agent_reset"
                )
            except _AmbiguousTemplateName:
                logger.debug("reset: copy name %r is ambiguous; leaving file and lineage", name)
                return
            if copy_path is None:
                # File already gone — nothing left that could appear shared.
                with contextlib.suppress(Exception):
                    agent_state.prune(name)
                return
            copy_file = copy_path
            if not _spec_path_is_safe(copy_file, agents_dir):
                return
            # This crew was rebound to the source above, so a binding still
            # naming the copy is another crew's: the locked helper keeps file
            # and lineage in that case, atomically against concurrent binding
            # writes. Stem included: it resolves this file.
            if (
                _unlink_copy_unless_referenced(
                    copy_file, agents_dir, name, _copy_name, copy_file.stem
                )
                != "deleted"
            ):
                return
            with contextlib.suppress(Exception):
                agent_state.prune(name)

        await asyncio.to_thread(_delete_copy)
    clear_list_agents_cache()
    state.push_refresh("agents")
    return web.json_response({"ok": True, "template": origin_name})
