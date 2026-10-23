"""``/api/members/{slug}/import-files``: write (and take back) an imported crewmate's files.

rfc-crewmate-share.md §5.4 steps 3 and 4. The ``crewmate.import`` card's file
step runs here, once the create has made the crewmate. It is owner-only and
answers only an admitted card step (``change_card_middleware`` arms
:data:`REQ_CARD_IMPORT_FILES`), so no direct call, app token or agent reaches it.

``POST`` re-reads the staged copy the card names (:mod:`kiro_crew.crewmate_stage`;
a sha256 mismatch refuses), re-runs every bundle check, and writes:

* each bundled skill into the new crewmate's own workspace, under
  ``.kiro/skills/<name>/`` (the tree its sessions read, behind the owner's
  project-skill trust), never into ``~/.kiro/crew/skills/``; a skill folder
  that already exists there refuses the step;
* each cron script into the crewmate's own ``<config dir>/crons/imported/<member id>/``
  with ``O_EXCL`` under the first free name (``<file>-2.py``), and its cron, bound
  to the new crewmate and created paused, pointing at that name. The launcher
  puts a script's own folder on ``sys.path``, so a carried ``sqlite3.py`` in the
  shared ``crons/`` would run inside any other job that imports ``sqlite3``; in
  its own folder it is reachable only from this crewmate's scripts;
* the picture, through the avatar commit the profile editor uses.

Anything it wrote is taken back if a later write in the same step fails. What it
wrote is recorded (``crewmate_stage.write_ledger``) so ``DELETE`` removes exactly
those files and crons, and refuses (409) if one of them changed since; each file's
digest is checked again right before it is removed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import crewmate_stage

logger = logging.getLogger(__name__)

#: Set by ``change_card_middleware`` on an admitted ``crewmate.import`` card step
#: to this route; never from a request. Without it the route refuses.
REQ_CARD_IMPORT_FILES = "card_import_files"

_FREE_NAME_TRIES = 99

#: Under ``<config dir>/crons/``: one folder per imported crewmate, by member id.
IMPORTED_SCRIPTS_DIR = "imported"


class _Refused(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _deny(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


async def _admit(request: web.Request, operation: str) -> tuple[str, dict[str, Any]]:
    """The owner's own card step, its slug and body; or :class:`_Refused`."""
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
    from kiro_crew.members import MemberSlugError, validate_slug

    if request.get("internal_auth") is True or request.get("app"):
        raise _Refused(403, "owner_only", "owner only")
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        raise _Refused(denied.status, "owner_only", "owner only")
    if not request.get(REQ_CARD_IMPORT_FILES):
        raise _Refused(403, "card_only", "only an import card's press writes these files")
    slug = request.match_info["slug"]
    try:
        validate_slug(slug)
    except MemberSlugError:
        raise _Refused(400, "invalid_member_slug", "invalid member slug") from None
    try:
        body = await request.json()
    except Exception:
        raise _Refused(400, "invalid_json", "invalid JSON body") from None
    if not isinstance(body, dict) or not isinstance(body.get("member"), str):
        raise _Refused(400, "invalid_body", "body must name the member")
    return slug, body


def _member(slug: str, member: str) -> tuple[str, Any, Any]:
    """``(crew key, record, config)`` of the crewmate with member id *slug*, named *member*."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import member_config_for_id

    cfg = KiroCrewConfig.load()
    try:
        key, agent = member_config_for_id(cfg, slug)
    except Exception:
        raise _Refused(404, "member_not_found", "no crewmate with that id") from None
    if key != member:
        raise _Refused(409, "member_slug_mismatch", "the member does not match its id")
    return key, agent, cfg


def _skills_root(cfg: Any, agent: Any) -> Path:
    """``<the crewmate's own workspace>/.kiro/skills``, refused if any part is a link."""
    from kiro_crew.config.loader import workspace_dir_from_entry

    entry = cfg.workspaces.get(agent.workspace)
    if entry is None:
        raise _Refused(409, "workspace_missing", "the crewmate has no workspace of its own")
    workspace = workspace_dir_from_entry(entry)
    for part in (workspace, workspace / ".kiro", workspace / ".kiro" / "skills"):
        if part.is_symlink():
            raise _Refused(409, "workspace_link", "the crewmate's workspace holds a link")
    root = workspace / ".kiro" / "skills"
    real_ws = os.path.realpath(workspace)
    if os.path.commonpath([real_ws, os.path.realpath(root)]) != real_ws:
        raise _Refused(409, "workspace_link", "the skills folder is outside the workspace")
    return root


def _write_new(path: Path, data: bytes) -> None:
    """Create *path* with *data*; refuse an existing name and never follow a link."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
    except BaseException:
        # This call made the name, so a half-written file is ours to remove.
        path.unlink(missing_ok=True)
        raise


def _free_script(crons_dir: Path, file: str, data: bytes) -> Path:
    """Write a script under *file*, or the first free ``<stem>-N.py``; never replace one."""
    stem = file[: -len(".py")]
    for n in range(1, _FREE_NAME_TRIES + 1):
        target = crons_dir / (file if n == 1 else f"{stem}-{n}.py")
        try:
            _write_new(target, data)
            return target
        except FileExistsError:
            continue
    raise _Refused(409, "script_name_taken", f"no free name for '{file}'")


def _scripts_dir(slug: str) -> Path:
    """``<config dir>/crons/imported/<slug>``, made 0700; refused if any part is a link."""
    from kiro_crew.config.paths import config_dir

    crons_dir = config_dir() / "crons"
    parent = crons_dir / IMPORTED_SCRIPTS_DIR
    target = parent / slug
    for part in (crons_dir, parent, target):
        if part.is_symlink():
            raise _Refused(409, "scripts_dir_link", "the crewmate's script folder holds a link")
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    real_crons = os.path.realpath(crons_dir)
    if os.path.commonpath([real_crons, os.path.realpath(target)]) != real_crons:
        raise _Refused(409, "scripts_dir_link", "the script folder is outside crons/")
    return target


def _drop_empty_dir(path: Path) -> None:
    try:
        path.rmdir()  # only when nothing else is in it
    except OSError:
        pass


def _write_files(slug: str, member: str, sha: str) -> dict[str, Any]:
    """Re-read and re-check the staged copy, then write its skills and scripts."""
    from kiro_crew import change_card_catalog as catalog
    from kiro_crew.crewmate_bundle import crewmate_import_params, read_crewmate_file

    try:
        data = crewmate_stage.load(sha)
    except crewmate_stage.StageError as exc:
        raise _Refused(409, exc.code, str(exc)) from None
    if crewmate_stage.read_ledger(slug) is not None:
        raise _Refused(409, "already_written", "this crewmate's files were already written")
    try:
        files = read_crewmate_file(data)
        params = crewmate_import_params(files.bundle, files)
    except catalog.CardCatalogError as exc:
        raise _Refused(409, exc.code, exc.message) from None
    key, agent, cfg = _member(slug, member)
    # Filled as each file lands, so a failure part way takes back exactly
    # those files, and only while they still hold the bytes written.
    record: dict[str, Any] = {"member": key, "skills": [], "scripts": [], "crons": []}
    script_map: dict[str, str] = {}
    try:
        if files.skills:
            root = _skills_root(cfg, agent)
            taken = [n for n in files.skills if (root / n).exists() or (root / n).is_symlink()]
            if taken:
                raise _Refused(409, "skill_exists", f"a skill named '{taken[0]}' is already there")
            root.mkdir(parents=True, exist_ok=True)
            for name, folder in sorted(files.skills.items()):
                skill_dir = root / name
                skill_dir.mkdir(mode=0o700)
                digests: dict[str, str] = {}
                record["skills"].append({"dir": str(skill_dir), "files": digests})
                for path, blob in sorted(folder.items()):
                    _write_new(skill_dir / path, blob)
                    digests[path] = hashlib.sha256(blob).hexdigest()
        if files.scripts:
            scripts_dir = _scripts_dir(slug)
            record["scripts_dir"] = str(scripts_dir)
            for file, blob in sorted(files.scripts.items()):
                target = _free_script(scripts_dir, file, blob)
                script_map[file] = str(target)
                sha_file = hashlib.sha256(blob).hexdigest()
                record["scripts"].append({"path": str(target), "sha256": sha_file})
    except BaseException:
        _take_back(record)
        raise
    return {
        "key": key,
        "record": record,
        "script_crons": [
            {
                **job,
                "script": f"{script_map[job['script'].partition(':')[0]]}:"
                f"{job['script'].partition(':')[2]}",
            }
            for job in params.get("crons") or ()
            if "script" in job
        ],
        "avatar": files.avatar,
        "member_id": slug,
    }


def _job_revision(job: Any) -> str:
    """The schedule fingerprint a card Undo compares (``change_cards.cron_job_revision``)."""
    from kiro_crew.dashboard.change_cards import cron_job_revision

    return str(cron_job_revision(job))


async def _add_script_crons(
    state: Any, slug: str, jobs: list[dict[str, Any]], ids: list[str], revisions: dict[str, str]
) -> None:
    """Each script cron, bound to the new crewmate and created paused in its one write.

    Each id lands in *ids* as its job is made, so a failure later in the loop
    still finds every cron this step made.
    """
    from kiro_crew.change_card_catalog import DEFAULT_CREWMATE_TEMPLATE
    from kiro_crew.cron_script import resolve_script_path

    for job in jobs:
        resolve_script_path(job["script"])  # refuses a path outside crons/
        made = await state.crons.add_job_async(
            job["name"],
            job.get("message", ""),
            cron_expr=job["cron_expr"],
            timezone=job.get("timezone", ""),
            script=job["script"],
            agent_id=DEFAULT_CREWMATE_TEMPLATE,
            member_id=slug,
            enabled=False,
        )
        ids.append(made.id)
        revisions[made.id] = _job_revision(made)


async def _set_avatar(key: str, ext: str, blob: bytes) -> str:
    """Stage, promote and commit the picture through the profile editor's own path."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.crewmate_carried import avatar_token
    from kiro_crew.dashboard.chat_utils_awaits import drained_to_thread
    from kiro_crew.dashboard.handlers.agents import (
        _avatar_stem,
        _avatars_dir,
        _commit_promoted_avatar,
        _get_config_lock,
        _promote_pending_avatar,
        _rollback_promoted_avatar,
    )
    from kiro_crew.memory_stores import persist_member_config

    def _stage() -> None:
        d = _avatars_dir()
        d.mkdir(parents=True, exist_ok=True)
        staged = d / f"{_avatar_stem(key)}.pending.{ext}"
        tmp = staged.with_suffix(f".{ext}.tmp-{uuid.uuid4().hex[:8]}")
        try:
            tmp.write_bytes(blob)
            os.replace(tmp, staged)
        finally:
            tmp.unlink(missing_ok=True)

    async with _get_config_lock():
        cfg = await asyncio.to_thread(KiroCrewConfig.load)
        agent = cfg.agents.get(key)
        if agent is None:
            raise _Refused(404, "member_not_found", "no crewmate with that name")
        await drained_to_thread(_stage)
        promoted = await drained_to_thread(_promote_pending_avatar, key, avatar_token(blob))
        if promoted is None:
            raise _Refused(409, "avatar_file_missing", "the picture could not be staged")
        stamp, pin = promoted
        prior = agent.avatar.get("file")
        agent.avatar = {"kind": "image", "v": stamp, "file": pin}
        try:
            await drained_to_thread(
                lambda: persist_member_config(
                    cfg, key, expected_store=agent.memory_store, changed_fields={"avatar"}
                )
            )
        except BaseException:
            await drained_to_thread(_rollback_promoted_avatar, key, pin, prior)
            raise
        await drained_to_thread(_commit_promoted_avatar, key, pin)
    return pin


async def api_member_import_files(request: web.Request) -> web.Response:
    """POST — write the carried files of the staged copy the card names."""
    try:
        slug, body = await _admit(request, "crewmate.import_files")
        sha = body.get("sha256")
        if set(body) != {"member", "sha256"} or not isinstance(sha, str):
            raise _Refused(400, "invalid_body", "body is {member, sha256}")
        state = request.app["state"]
        done = await asyncio.to_thread(_write_files, slug, body["member"], sha)
        record = done["record"]
        try:
            record["cron_revisions"] = {}
            await _add_script_crons(
                state, slug, done["script_crons"], record["crons"], record["cron_revisions"]
            )
            if done["avatar"] is not None:
                record["avatar"] = await _set_avatar(done["key"], *done["avatar"])
            await asyncio.to_thread(crewmate_stage.write_ledger, slug, record)
        except BaseException:
            # The owner may have edited a cron or a file during the awaits above:
            # take back only what still reads as this step wrote it.
            from kiro_crew.dashboard.change_cards import cron_job_revision

            revisions = record.get("cron_revisions") or {}
            # A cron the owner changed meanwhile stays, and so does its script.
            keep: set[str] = set()
            for job_id in record.get("crons") or ():
                try:
                    outcome = await state.crons.remove_job_if_async(
                        job_id,
                        expected_revision=revisions.get(job_id, ""),
                        revision_of=cron_job_revision,
                        actor="dashboard",
                        source="crewmate.import",
                    )
                except Exception:
                    outcome = "changed"  # unknown: keep what it may still run
                if outcome not in ("removed", "missing"):
                    keep |= _scripts_of_job(record, job_id, done["script_crons"])
            await asyncio.to_thread(_take_back, record, keep)
            raise
    except _Refused as exc:
        return _deny(exc.status, exc.code, exc.message)
    if record["crons"] and hasattr(state, "push_refresh"):
        state.push_refresh("crons")
    return web.json_response(
        {
            "ok": True,
            "skills": len(record["skills"]),
            "scripts": [Path(s["path"]).name for s in record["scripts"]],
            "crons": len(record["crons"]),
            "avatar": bool(record.get("avatar")),
        }
    )


def _still_as_written(target: Path, sha: str) -> bool:
    """Whether *target* is still the file the import wrote; ``False`` when it is gone.

    Anything but the same regular file with the same bytes is a change the owner
    made, and refuses.
    """
    if not target.exists() and not target.is_symlink():
        return False  # already gone: nothing to take back
    if target.is_symlink() or not target.is_file():
        raise _Refused(409, "changed_since_apply", f"'{target.name}' changed after the import")
    if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
        raise _Refused(409, "changed_since_apply", f"'{target.name}' changed after the import")
    return True


def _check_unchanged(record: dict[str, Any]) -> None:
    """Refuse the Undo when any file the import wrote was changed since."""
    targets = [
        (Path(skill["dir"]) / path, sha)
        for skill in record.get("skills") or ()
        for path, sha in (skill.get("files") or {}).items()
    ]
    targets += [(Path(s["path"]), s["sha256"]) for s in record.get("scripts") or ()]
    for target, sha in targets:
        _still_as_written(target, sha)


def _remove_record(record: dict[str, Any]) -> None:
    """Remove each file the import wrote, checking its digest again right before.

    The Undo's first check ran before the awaited cron removals, so a file the
    owner saved in between is refused here, before anything is removed, and each
    file is checked once more as it is removed.
    """
    _check_unchanged(record)
    for skill in record.get("skills") or ():
        root = Path(skill["dir"])
        if root.is_symlink() or root.parent.is_symlink():
            # Swapped for a link since the check: never unlink through it.
            raise _Refused(409, "changed_since_apply", "the skill folder changed after the import")
        for path, sha in (skill.get("files") or {}).items():
            target = root / path
            if _still_as_written(target, sha):
                target.unlink()
        _drop_empty_dir(root)  # only when nothing else was added since
    for script in record.get("scripts") or ():
        target = Path(script["path"])
        if target.parent.is_symlink():
            raise _Refused(409, "changed_since_apply", "the script folder changed after the import")
        if _still_as_written(target, script["sha256"]):
            target.unlink()
    if record.get("scripts_dir"):
        _drop_empty_dir(Path(record["scripts_dir"]))


def _scripts_of_job(record: dict[str, Any], job_id: str, jobs: list[dict[str, Any]]) -> set[str]:
    """The script file the import's cron *job_id* runs; every script when unknown.

    ``record["crons"]`` lists the ids in the order *jobs* were made.
    """
    ids = list(record.get("crons") or ())
    if job_id in ids and ids.index(job_id) < len(jobs):
        return {str(jobs[ids.index(job_id)]["script"]).rpartition(":")[0]}
    return {str(s["path"]) for s in record.get("scripts") or ()}


def _take_back(record: dict[str, Any], keep: set[str] | frozenset[str] = frozenset()) -> None:
    """Best-effort rollback of a failed file step: remove each file that still
    holds the bytes it wrote, keep any the owner changed and any script in
    *keep* (a cron that stays still runs it), and remove a folder only once it
    is empty. The step reports the failure itself."""
    targets = [
        (Path(skill["dir"]) / path, sha)
        for skill in record.get("skills") or ()
        for path, sha in (skill.get("files") or {}).items()
    ]
    targets += [
        (Path(s["path"]), s["sha256"])
        for s in record.get("scripts") or ()
        if str(s["path"]) not in keep
    ]
    for target, sha in targets:
        try:
            if not target.parent.is_symlink() and _still_as_written(target, sha):
                target.unlink()
        except (_Refused, OSError):
            continue  # changed or unreadable: the owner's now
    for skill in record.get("skills") or ():
        _drop_empty_dir(Path(skill["dir"]))
    if record.get("scripts_dir"):
        _drop_empty_dir(Path(record["scripts_dir"]))


async def api_member_import_files_undo(request: web.Request) -> web.Response:
    """DELETE — take back exactly what the file step wrote for this crewmate."""
    try:
        slug, body = await _admit(request, "crewmate.import_files_undo")
        if set(body) != {"member"}:
            raise _Refused(400, "invalid_body", "body is {member}")
        try:
            record = await asyncio.to_thread(crewmate_stage.read_ledger, slug)
        except crewmate_stage.StageError as exc:
            raise _Refused(409, exc.code, str(exc)) from None
        if record is None:
            return web.json_response({"ok": True, "removed": 0})
        if record.get("member") != body["member"]:
            raise _Refused(409, "member_slug_mismatch", "the member does not match its id")
        await asyncio.to_thread(_check_unchanged, record)
        state = request.app["state"]
        scripts = {s["path"] for s in record.get("scripts") or ()}
        jobs = []
        for job_id in record.get("crons") or ():
            job = await state.crons.get_job_async(job_id)
            if job is None:
                continue
            # A cron the owner since edited, pointed elsewhere or switched on is
            # theirs now. Split on the LAST colon: a Windows path carries a drive colon.
            expected = (record.get("cron_revisions") or {}).get(job_id)
            if (
                str(job.script).rpartition(":")[0] not in scripts
                or getattr(job, "enabled", False)
                or expected != _job_revision(job)
            ):
                raise _Refused(
                    409, "changed_since_apply", "a cron it made changed after the import"
                )
            jobs.append(job_id)
        from kiro_crew.dashboard.change_cards import cron_job_revision

        for job_id in jobs:
            # Compare-and-delete under the cron store's lock: an edit that lands
            # after the check above is refused, never deleted.
            outcome = await state.crons.remove_job_if_async(
                job_id,
                expected_revision=(record.get("cron_revisions") or {}).get(job_id),
                revision_of=cron_job_revision,
                actor="dashboard",
                source="crewmate.import.undo",
            )
            if outcome == "changed":
                raise _Refused(
                    409, "changed_since_apply", "a cron it made changed after the import"
                )
        await asyncio.to_thread(_remove_record, record)
        await asyncio.to_thread(crewmate_stage.drop_ledger, slug)
    except _Refused as exc:
        return _deny(exc.status, exc.code, exc.message)
    return web.json_response({"ok": True})
