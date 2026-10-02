"""``GET/PATCH /api/agents/detail/{name}``: the template pane's read and its locked spec patch, which merges resources element-wise."""

from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.handlers.agents import (
        DASHBOARD_AUTHOR_AGENT_FILENAME,
        MAX_AGENT_SKILLS,
        TEMPLATE_DEFINITION_KEYS,
        CapabilityError,
        DashboardState,
        SharedAgentHomeRefused,
        SkillCatalogSnapshot,
        _AmbiguousTemplateName,
        _atomic_json_write,
        _declined_foreign_spec_write,
        _get_config_lock,
        _is_confirmed_managed_dashboard_author,
        _read_agent_spec,
        _read_session_key,
        _require_owner,
        _spec_path_is_safe,
        agent_skill_keys,
        agent_skill_views,
        agent_state,
        agents_spec_lock,
        apply_definition_patch,
        apply_skill_mapping,
        clear_list_agents_cache,
        clear_model_pin,
        discovery_executor,
        enumerate_skill_catalog,
        is_markdown_spec,
        iter_agent_spec_files,
        kiro_agents_dir_path,
        read_only_reason_for_path,
        require_unmanaged_template,
        sanitize_agent_config_governance,
        spec_model,
        spec_str,
        validate_definition_patch,
    )


def _agent_detail_candidates(name: str) -> list[tuple[Path, dict[str, Any]]]:
    """The user-level specs claiming *name* by declared name or stem, in scan order.

    A thread-side read for :func:`api_agent_detail`: the walk and the hardened
    per-file parse are filesystem work, and the handler only ever acts on the
    files that match.
    """
    matches: list[tuple[Path, dict[str, Any]]] = []
    for f in iter_agent_spec_files(kiro_agents_dir_path(), ordered=False):
        spec = _read_agent_spec(
            f,
            operation="api_agent_detail",
            source="dashboard",
        )
        if spec is None:
            continue
        if spec.get("name") == name or f.stem == name:
            matches.append((f, spec))
    return matches


def _merge_resources_delta(
    fresh: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    ordered: Sequence[str] = (),
) -> None:
    """Apply this patch's ``resources`` delta to the freshly-read spec, element-wise.

    ``after`` was built from a snapshot taken before the spec lock, so assigning it whole
    would drop a URI a concurrent writer added into *fresh* since. Only what this patch
    NAMED -- the URIs it removed, the ones it added, and the order it asked for -- may
    move.

    ``ordered`` is the managed ``skill://`` URIs the patch mapped, in the order it asked
    for. That order is re-applied within those URIs' own slots of the merged list and
    nowhere else: a ``file://`` glob or a hand-written wildcard the author interleaved
    between two skills keeps its index. The order is the ONLY thing read from
    ``ordered`` -- membership still comes from the delta above -- and it is applied to
    the URIs the merged list carries, so a URI a concurrent writer removed is never put
    back by a reorder that still names it.
    """

    def _uris(doc: dict[str, Any]) -> list[str]:
        """URI strings, with a malformed ``resources`` normalised to empty.

        Iterating a STRING yields characters and every one of them is a ``str``, so an
        unguarded comprehension would rewrite the value as a per-character list.
        """
        resources = doc.get("resources")
        if not isinstance(resources, list):
            return []
        return [r for r in resources if isinstance(r, str)]

    before_uris = _uris(before)
    after_uris = _uris(after)
    if before_uris == after_uris:
        # This patch named no resource change, so it may not rewrite the key at all: a
        # malformed value it never looked at must survive untouched rather than normalised.
        return
    removed = [r for r in before_uris if r not in after_uris]
    added = [r for r in after_uris if r not in before_uris]
    fresh_entries = fresh.get("resources")
    if not isinstance(fresh_entries, list):
        fresh_entries = []
    # Only the STRINGS this patch named may leave: an entry of any other shape is not
    # something this merge has an opinion about, so it is carried through unread.
    kept = [e for e in fresh_entries if not isinstance(e, str) or e not in removed]
    merged = _reorder_named(kept + [r for r in added if r not in kept], ordered)
    if merged:
        fresh["resources"] = merged
    else:
        # Same reason the mapping writer drops the key rather than writing []: an empty
        # list suppresses the shipped steering defaults.
        fresh.pop("resources", None)


def _reorder_named(entries: list[Any], ordered: Sequence[str]) -> list[Any]:
    """Refill the slots of the URIs *ordered* names with those URIs, in its order.

    A slot is the first index at which a named URI occurs in *entries*; every other
    entry -- a ``file://`` glob, an unmanaged ``skill://`` wildcard, a non-string, a
    further copy of a named URI -- keeps its index. Only URIs *entries* carries take a
    slot, so a named URI that is absent is skipped, never inserted, and the slots and
    the URIs refilling them always count the same.
    """
    carried = {e for e in entries if isinstance(e, str)}
    present = [u for u in dict.fromkeys(ordered) if u in carried]
    if not present:
        return entries
    named = set(present)
    slots: list[int] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if isinstance(entry, str) and entry in named and entry not in seen:
            seen.add(entry)
            slots.append(index)
    reordered = list(entries)
    for index, uri in zip(slots, present):
        reordered[index] = uri
    return reordered


async def api_agent_detail(request: web.Request) -> web.Response:
    """GET/PATCH /api/agents/detail/{name} — view or update agent config."""
    name = request.match_info["name"]
    if request.method != "GET":
        denied = await _require_owner(request, f"agent_detail.{request.method.lower()}")
        if denied is not None:
            return denied
    # Parse body early so a malformed body returns 400, not 404 from the file loop.
    patch_body = None
    if request.method == "PATCH":
        try:
            patch_body = await request.json()
        except ValueError:
            return web.json_response({"error": "invalid JSON"}, status=400)
        # Valid JSON is not necessarily an object. A top-level array makes
        # ``"skills" in patch_body`` a LIST-membership test (true for
        # ``["skills"]``), and the subscript that follows then raises TypeError
        # -> HTTP 500. Reject the shape once, here, rather than per-field.
        if not isinstance(patch_body, dict):
            return web.json_response({"error": "body must be a JSON object"}, status=400)

    state: DashboardState = request.app["state"]
    # The directory walk and the per-file parse run in a thread: a large agents
    # directory must not stall every other request on the loop. Only the specs
    # that claim *name* come back, in scan order, so the body below keeps its
    # skip-to-next-file shape over exactly the files it would have acted on.
    candidates = await asyncio.to_thread(_agent_detail_candidates, name)
    if request.method == "PATCH" and patch_body is not None and len(candidates) > 1:
        # A PATCH rewrites ONE file -- model, skills, prompt, tools alike. Two
        # files claiming the name (``atlas.json`` beside ``SomePkg-atlas.json``,
        # or a hand-edited declared name colliding with another file's stem)
        # would be resolved by unordered scan order, so the file the roster
        # showed and the file overwritten could differ. Refused for every key,
        # like the fork/publish resolvers refuse ``_AmbiguousTemplateName``;
        # the same check runs again under the write lock below.
        return web.json_response(
            {
                "error": f"'{name}' matches more than one template file; rename one first.",
                "code": "ambiguous_template_name",
            },
            status=409,
        )
    for f, spec in candidates:
        # Two-step so ``data`` stays typed ``dict`` for the PATCH branch's
        # re-read below, which reassigns it from a raw ``json.loads``.
        data = spec
        # The try stays even though the parse moved out: the DELETE/PATCH
        # bodies still raise the caught pair mid-flight (the PATCH re-read
        # under the config lock, unlink), and those were -- and remain --
        # skip-to-next-file.
        try:
            if request.method != "GET" and is_markdown_spec(f):
                # A markdown spec is one hand-authored document. Serializing
                # a JSON object over it would drop the prompt body and every
                # field this handler does not model, so it is read-only here.
                return web.json_response(
                    {
                        "error": (
                            f"agent '{name}' is defined in markdown ({f.name}); "
                            "edit the file directly"
                        ),
                        "code": "markdown_spec_readonly",
                    },
                    status=409,
                )
            if request.method == "PATCH" and patch_body is not None:
                try:
                    # Either lookup spelling can resolve this same file; a
                    # hand-edited name cannot hide its enrolled stem.
                    for identity in dict.fromkeys((f.stem, spec_str(data, "name") or f.stem)):
                        await asyncio.to_thread(require_unmanaged_template, identity)
                except CapabilityError as exc:
                    return web.json_response(
                        {"error": exc.code, "code": exc.code}, status=exc.status
                    )
                if "skills" in patch_body:
                    raw_skills = patch_body["skills"]
                    if not isinstance(raw_skills, list) or not all(
                        isinstance(s, str) for s in raw_skills
                    ):
                        return web.json_response(
                            {"error": "skills must be a list of strings"}, status=400
                        )
                    if len(raw_skills) > MAX_AGENT_SKILLS:
                        return web.json_response(
                            {"error": f"at most {MAX_AGENT_SKILLS} skills per agent"},
                            status=400,
                        )
                if TEMPLATE_DEFINITION_KEYS & patch_body.keys():
                    # The templates tab's definition edit (prompt, description,
                    # tools). Shape-checked here; refused for a spec the tab
                    # cannot own -- a package or runtime file would be reverted
                    # on its next install, a private copy belongs to its crew's
                    # pane. ``model`` / ``skills`` keep their existing reach: the
                    # crew pane writes those onto private copies.
                    problem = validate_definition_patch(patch_body)
                    if problem is not None:
                        return web.json_response(
                            {"error": problem, "code": "invalid_definition"}, status=400
                        )
                    read_only = await asyncio.to_thread(read_only_reason_for_path, f)
                    if read_only is not None:
                        return web.json_response(
                            {
                                "error": f"Template '{name}' is read-only ({read_only})",
                                "code": "template_read_only",
                                "reason": read_only,
                            },
                            status=409,
                        )
                mapped: list[str] = []
                mapped_uris: list[str] = []
                # The catalog walk the mapping validated the keys against, with its
                # staleness stamp; the reply is resolved off the written spec against
                # it, so a skills PATCH walks the skill roots once unless they moved.
                snapshot: SkillCatalogSnapshot | None = None
                session_key = _read_session_key(request)
                loop = asyncio.get_running_loop()
                async with _get_config_lock():
                    # Re-read under the lock: the copy above was read before
                    # the lock and a concurrent PATCH may have superseded it.
                    # The branch writes this data back, so bind the same
                    # agents directory and apply the stricter no-symlink /
                    # no-escape fence before the hardened read.  Keep the
                    # filesystem work off the event loop while the shared
                    # config lock is held.
                    agents_dir = kiro_agents_dir_path()

                    def _reread_under_lock(
                        spec_file: Path = f,
                        root: Path = agents_dir,
                    ) -> dict[str, Any] | None:
                        if not _spec_path_is_safe(spec_file, root):
                            return None
                        return _read_agent_spec(
                            spec_file,
                            operation="api_agent_detail",
                            source="dashboard",
                        )

                    reread_data = await asyncio.to_thread(_reread_under_lock)
                    if reread_data is None:
                        return web.json_response(
                            {
                                "error": f"'{name}' changed on disk during update; retry.",
                                "code": "agent_changed",
                            },
                            status=409,
                        )
                    data = reread_data
                    # Pristine snapshot: the locked write below re-reads the
                    # CURRENT disk state and re-applies only the keys this
                    # PATCH changed relative to this snapshot, so it cannot
                    # clobber a concurrent refresh's writes with stale data.
                    before_patch = copy.deepcopy(reread_data)
                    # `spec_str` for the same reason as `declared` above: a
                    # hand-edited spec can carry a structured (non-string)
                    # "name", which would crash the sidecar helper's dict
                    # lookup with an unhashable key.
                    agent_name = spec_str(data, "name") or name
                    # Skills FIRST, before any state mutation. The mapping can
                    # reject the request (unknown key -> 400) and the model
                    # branch below writes the agent_state sidecar; doing model
                    # first meant a rejected combined PATCH still froze the
                    # model against future shipped-default bumps.
                    #
                    # Offloaded to the discovery pool: the mapping enumerates
                    # the skill roots (see enumerate_skill_catalog), which on a
                    # large or network-backed catalog is enough filesystem work
                    # to stall the event loop — the same reason /api/skills and
                    # /api/agents/installed run off the loop.
                    if "skills" in patch_body:
                        # The applied keys are the REQUEST's view of the mapping and are
                        # not read again: the reply is resolved off the spec as written
                        # under the lock, below, against this same catalog walk. The URIs
                        # steer the merge's reorder.
                        _applied, unknown, mapped_uris, snapshot = await loop.run_in_executor(
                            discovery_executor(),
                            apply_skill_mapping,
                            data,
                            f,
                            state,
                            list(patch_body["skills"]),
                            session_key,
                        )
                        if unknown:
                            return web.json_response(
                                {"error": "unknown skills", "skills": unknown[:20]},
                                status=400,
                            )
                    else:
                        mapped = await loop.run_in_executor(
                            discovery_executor(),
                            agent_skill_keys,
                            data,
                            f,
                            state,
                            session_key,
                        )
                        mapped_uris = []

                    # mypy checks this owner before the handlers module, defers this closure
                    # and loses the enclosing function's narrowing of ``patch_body`` here.
                    def _locked_overwrite() -> list[str]:
                        # Same spec lock as fork/publish and the background
                        # fork refresh — and a full read-merge-write inside
                        # it: our `data` snapshot was taken before the lock,
                        # so a concurrent refresh may have sanitized away a
                        # ceiling-rejected grant since; writing the snapshot
                        # verbatim would restore it. Merge only the keys THIS
                        # patch changed onto the fresh read, then run the
                        # mandated whole-config governance funnel immediately
                        # before persisting (same contract as
                        # _write_spec_file and the PUT handler). Returns the
                        # skills the WRITTEN spec maps, for the reply.
                        with agents_spec_lock(f.parent):
                            # Ownership BEFORE any bookkeeping, and inside this
                            # lock: the model branch below writes a per-home
                            # sidecar, and the spec write that can refuse comes
                            # after it. Refusing only there answered 409 with
                            # the sidecar already flipped, so the pin recorded a
                            # model the spec on disk never carried. Asked here
                            # the request mutates nothing at all.
                            if _declined_foreign_spec_write(f):
                                raise SharedAgentHomeRefused(
                                    f"{f} belongs to another Kiro Crew data home"
                                )
                            # The pre-lock ambiguity check re-run where it
                            # decides: a second claimant that landed after the
                            # scan (a package install) must refuse, not let
                            # the stale single match be overwritten.
                            if [c for c, _spec in _agent_detail_candidates(name)] != [f]:
                                raise _AmbiguousTemplateName(name)
                            fresh = _read_agent_spec(
                                f, operation="api_agent_detail", source="dashboard"
                            )
                            if fresh is None:
                                raise FileNotFoundError(f)
                            # Snapshot the PRE-merge bytes: the dashboard-author ownership
                            # digest is renewed below only when this pre-write content was
                            # ALREADY our confirmed managed write, never for a user file at
                            # the stem (which would stamp the user's bytes as managed and let
                            # the next rebuild overwrite them).
                            pre_write = copy.deepcopy(fresh)
                            # Check the file stem AND its fresh declared name
                            # before ALL bookkeeping; the earlier name can be
                            # stale. Keep the spec -> sidecar lock order.
                            for identity in dict.fromkeys(
                                (f.stem, spec_str(fresh, "name") or f.stem)
                            ):
                                require_unmanaged_template(identity)
                            if "model" in patch_body:  # type: ignore[operator]
                                data["model"] = patch_body["model"] or None  # type: ignore[index]
                                if data["model"] is None:
                                    clear_model_pin(data, agent_name)
                                else:
                                    agent_state.set_model_managed(agent_name, False)
                            apply_definition_patch(data, patch_body)  # type: ignore[arg-type]
                            agent_state.lift_and_strip_bookkeeping(data, agent_name)
                            for key, value in data.items():
                                if key == "resources":
                                    # Merged element-wise below: assigning this list
                                    # whole would drop a concurrent writer's addition.
                                    continue
                                if key not in before_patch or before_patch[key] != value:
                                    fresh[key] = value
                            for key in before_patch:
                                if key not in data and key != "resources":
                                    fresh.pop(key, None)
                            _merge_resources_delta(fresh, before_patch, data, mapped_uris)
                            sanitize_agent_config_governance(fresh)
                            # Atomic replace: a direct write truncates first,
                            # so ENOSPC mid-write would destroy the existing
                            # template. Same tmp+rename helper as the fork
                            # refresh and install paths. An authorized model/skills edit of
                            # the OWNED dashboard-author spec renews the ownership digest ONLY
                            # when the PRE-write content was already our confirmed managed
                            # spec -- a user file at the once-user-creatable stem is never
                            # stamped. Two-phase (begin pending -> write -> finalize) so a
                            # crash leaves bytes and record in step and the installer keeps
                            # re-filtering against a tightened ceiling. The helper and the
                            # owned filename are composed GLOBALS (bound from the agents
                            # facade), so this owner submodule imports no materialization owner.
                            if f.stem == Path(DASHBOARD_AUTHOR_AGENT_FILENAME).stem and (
                                _is_confirmed_managed_dashboard_author(pre_write)
                            ):
                                agent_state.begin_managed_write(
                                    f.stem,
                                    agent_state.spec_digest(fresh),
                                    current=agent_state.spec_digest(pre_write),
                                )
                                _atomic_json_write(f, fresh)
                                agent_state.finalize_managed_write(f.stem)
                            else:
                                _atomic_json_write(f, fresh)
                        if snapshot is None:
                            # No skills in this patch: the mapping is the pre-lock read's.
                            return mapped
                        # Report the skills the WRITTEN spec maps -- the view a GET answers --
                        # rather than the request: the locked merge applies the request onto
                        # the fresh read, so the two differ whenever a concurrent writer
                        # removed or added a URI in between, and the skills editor takes this
                        # reply as its next state. Resolved against the catalog walk the
                        # mapping validated the keys with, re-walked only when a stat of the
                        # directories that walk read says the roots moved since: a skill
                        # a co-owner installed AND mapped in between is not in the
                        # snapshot, and a reply missing it would have the editor's next
                        # toggle unmap it. Still off the loop, since a hand-authored URI's
                        # inversion resolves paths.
                        catalog = snapshot.entries
                        if snapshot.changed():
                            catalog = enumerate_skill_catalog(state, session_key)
                        return agent_skill_keys(fresh, f, state, catalog=catalog)

                    try:
                        mapped = await asyncio.to_thread(_locked_overwrite)
                    except CapabilityError as exc:
                        return web.json_response(
                            {"error": exc.code, "code": exc.code}, status=exc.status
                        )
                    except _AmbiguousTemplateName:
                        return web.json_response(
                            {
                                "error": f"'{name}' matches more than one template file; "
                                "rename one first.",
                                "code": "ambiguous_template_name",
                            },
                            status=409,
                        )
                    except SharedAgentHomeRefused:
                        # The edit was DISCARDED, so it must not be reported as
                        # saved. This instance's data home does not own the
                        # agents directory the template lives in, so writing
                        # would re-pin another gateway's spec to this one's venv
                        # and home. 409: the state on disk belongs to a
                        # different owner, which retrying cannot change —
                        # the remedy is to give this instance its own
                        # KIRO_HOME, and the gateway log names the directory.
                        return web.json_response(
                            {
                                "error": (
                                    f"'{name}' is owned by another Kiro Crew data home; "
                                    "this instance will not rewrite it."
                                ),
                                "code": "agent_home_not_owned",
                            },
                            status=409,
                        )
                    except FileNotFoundError:
                        return web.json_response(
                            {
                                "error": f"'{name}' changed on disk during update; retry.",
                                "code": "agent_changed",
                            },
                            status=409,
                        )
                # The list_agents() cache keys on a (count, newest-mtime-ns)
                # signature; two writes inside the same mtime granularity
                # would otherwise serve a stale skill list.
                clear_list_agents_cache()
                state.push_refresh("agents")
                return web.json_response(
                    {"ok": True, "model": data.get("model", ""), "skills": mapped}
                )
            # ``skills`` / ``unmanaged_skills`` are computed, response-only
            # views of ``resources`` — never written back into the spec
            # (kiro-cli rejects unknown fields and drops the agent). One
            # catalog walk for both, off the event loop (filesystem-heavy).
            keys, unmanaged_uris = await asyncio.get_running_loop().run_in_executor(
                discovery_executor(),
                agent_skill_views,
                data,
                f,
                state,
                _read_session_key(request),
            )
            # The spec is otherwise passed through verbatim, so mask the one
            # value in it that is a credential (a pre-registered Connections
            # client's projected secret); this read is not owner-gated.
            from kiro_crew.mcp_utils import redact_oauth_client_secrets

            return web.json_response(
                {
                    **redact_oauth_client_secrets(data),
                    # The rest of the spec is passed through verbatim, but
                    # these two are CONSUMED as display text by the detail
                    # panel. A foreign spec's structured value rendered as a
                    # React child throws error #31 and blanks the whole tab,
                    # so both are coerced on the same "non-string means
                    # absent" rule list_agents() uses.
                    "description": spec_str(data, "description"),
                    "model": spec_model(data),
                    "skills": keys,
                    "unmanaged_skills": unmanaged_uris,
                }
            )
        except (json.JSONDecodeError, OSError):
            continue
    # "default" is the built-in agent with no config file
    if name == "default":
        if request.method != "GET":
            return web.json_response({"error": "cannot modify built-in default agent"}, status=400)
        return web.json_response({"name": "default", "model": ""})

    return web.json_response({"error": "not found"}, status=404)
