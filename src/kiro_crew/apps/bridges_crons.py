"""An app's cron jobs: the manifest file, and promoting them into the running CronService.

The persisted cron definitions (``load_app_cron_defs``), the manifest digest stamped on
an app job and the resync of a job whose manifest entry changed, registration with the
CronService (``register_app_crons_with_service``), deregistration, and the disarm and
reconcile passes the execution decision drives.

Part of the ``kiro_crew.apps.bridges`` family: callers and tests reach every name here
as ``kiro_crew.apps.bridges.<name>``, and a patch there reaches this module too
(:mod:`kiro_crew._facade_mirror`). This module never imports the facade.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable

from kiro_crew import cron as cron_seams
from kiro_crew.apps.bridges_sources import _namespace, _registration_denied, _registration_source
from kiro_crew.apps.cron_sdk import CronSDK
from kiro_crew.apps.execution import shipped_builtin_app_root
from kiro_crew.apps.manager import app_dir, list_apps
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.atomic_write import atomic_write
from kiro_crew.cron import (
    CronPendingMismatch,
    CronStoreBusy,
    CronStoreUnreadable,
    lookup_cron_folder_id,
)
from kiro_crew.cron_script import resolve_script_path
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.apps.bridges")


# ---------------------------------------------------------------------------
# Cron registration (deferred — writes a manifest for the CronService)
# ---------------------------------------------------------------------------

_CRON_MANIFEST_NAME = "app-crons.json"


def _app_crons_path(app_name: str) -> Path:
    """Path to the app's cron manifest within its install directory."""
    return app_dir(app_name) / _CRON_MANIFEST_NAME


def _cron_defs_from_manifest(
    app_name: str,
    manifest: AppManifest,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Build scheduler definitions from an authoritative app manifest."""
    cron_defs: list[dict[str, Any]] = []
    registered: list[str] = []
    for cron in manifest.crons:
        namespaced = _namespace(app_name, cron.name)
        cron_defs.append(
            {
                "name": namespaced,
                "every": cron.every,
                "cron_expr": cron.cron_expr,
                "agent": cron.agent,
                "message": cron.message,
                "command": cron.command,
                "script": cron.script,
                "app": app_name,
                "agent_sequence": cron.agent_sequence,
                "env": cron.env,
                "persistent_session": cron.persistent_session,
                "silent": cron.silent,
                "enabled": cron.enabled,
                "timezone": cron.timezone,
                "skip_dates": cron.skip_dates,
                "folder": cron.folder,
                "timeout": cron.timeout,
                "timeout_secs": cron.timeout_secs,
                "strict_schedule": cron.strict_schedule,
            }
        )
        registered.append(namespaced)
    return cron_defs, registered


def _register_crons(app_name: str, manifest: AppManifest) -> list[str]:
    """Write app cron definitions to a manifest file for later CronService pickup.

    The actual CronService registration happens at enable time via
    ``register_app_crons_with_service()``. This just persists the definitions
    so they survive restarts.

    Returns list of namespaced cron names.
    """
    cron_defs, registered = _cron_defs_from_manifest(app_name, manifest)
    if not cron_defs:
        return []

    path = _app_crons_path(app_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: the scheduler reads this file at every start, so a write cut off
    # part way must leave the previous definitions in place, not a torn file.
    atomic_write(path, json.dumps(cron_defs, indent=2))
    logger.info("Wrote %d cron definition(s) for app %s", len(cron_defs), app_name)
    return registered


def _deregister_crons(app_name: str) -> int:
    """Remove the app's cron manifest."""
    path = _app_crons_path(app_name)
    if path.is_file():
        path.unlink()
        logger.info("Removed cron manifest for app %s", app_name)
        return 1
    return 0


def load_app_cron_defs(app_name: str) -> list[dict[str, Any]]:
    """Load persisted cron definitions for an app (used by CronService bridge).

    The return type is a promise to the caller, so it is enforced rather than
    assumed. ``app-crons.json`` lives in the app's INSTALL directory, which is
    ordinary user-writable state -- a hand-edit, a partial restore, or an app
    that writes its own file can leave valid JSON that is not a list of
    objects. That parses cleanly, so catching ``JSONDecodeError`` does not see
    it, and the value flows into ``register_app_crons_with_service``'s
    ``for d in defs: d.get("name", "")`` -- which raises ``AttributeError`` on
    a string (iterating a JSON object yields its keys) and ``TypeError`` on a
    scalar, from OUTSIDE the per-job ``try`` that makes one bad cron skippable.

    Two levels, because they fail differently:

    - a non-list top level is the whole file being wrong, and is treated
      exactly like the unreadable case above -- no definitions, app enables
      without crons.
    - a non-object ENTRY is one bad row among good ones, and is skipped the
      way an entry whose registration raises already is, so the remaining
      crons still register.
    """
    path = _app_crons_path(app_name)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        # A manifest that exists but cannot be read (for example one left torn by
        # an interrupted write) is logged at WARNING and treated as no crons, so
        # none of the app's scheduled jobs from it are registered.
        logger.warning(
            "App %s: cron manifest %s could not be read (%s); ignoring it",
            app_name,
            path,
            type(exc).__name__,
        )
        return []
    if not isinstance(data, list):
        logger.warning(
            "App %s: cron manifest %s is not a JSON array (%s); ignoring it",
            app_name,
            path,
            type(data).__name__,
        )
        return []
    defs = [entry for entry in data if isinstance(entry, dict)]
    if len(defs) != len(data):
        logger.warning(
            "App %s: cron manifest %s has %d entry/entries that are not JSON objects; skipping them",
            app_name,
            path,
            len(data) - len(defs),
        )
    return defs


def _resolve_and_vet_app_script(
    script: str,
    app_root: Path,
    vet_script_file: Callable[[str], str | None],
) -> tuple[str, str, str | None]:
    """Resolve an app cron's script against its bundle and scan its body.

    One function so ``register_app_crons_with_service`` can hand BOTH filesystem
    steps to a single ``asyncio.to_thread`` call: resolution stats the script and
    may walk the builtin manifest sources, and the body scan reads up to
    ``_MAX_SCRIPT_SCAN_BYTES``. Splitting them across two offloads would pay two
    thread hops per job for one logically atomic check.

    Returns ``(file_path, func_name, error)``. ``error`` is the body scan's
    refusal string, or ``None`` when the script passes. Resolution failures
    propagate as ``PermissionError``/``FileNotFoundError``/``ValueError`` for the
    caller's existing handler, which audits them as a path rejection -- a
    distinction the caller keeps, so the SEL trail still separates "path refused"
    from "body refused".

    ``vet_script_file`` is injected rather than imported here because the import
    is deferred at the call site to break the ``mcp_cron`` -> ... -> ``bridges``
    cycle, and re-importing it inside a worker thread would reopen that.
    """
    file_path, func_name = resolve_script_path(script, app_root=app_root)
    return file_path, func_name, vet_script_file(file_path)


#: The ``app-crons.json`` keys an app's manifest OWNS on its registered job --
#: the ones :func:`register_app_crons_with_service` re-applies, one by one, when
#: the entry's value for that key changes. ``name`` and ``app`` are the job's
#: identity, not its content. ``enabled`` is deliberately absent: the manifest
#: value is the INITIAL pause state only, and whether the job is paused afterwards
#: belongs to the person (``user_paused``) and to the failure auto-pause, so an
#: author flipping it does not count as a change and never overrides either.
#:
#: The stamp (:func:`app_cron_manifest_digest`) is KEYED, one ``key=digest`` per
#: entry here, so a key added later is simply missing from older stamps and
#: counts as changed on its own; the other keys keep their per-field history.
_MANIFEST_OWNED_CRON_KEYS: tuple[str, ...] = (
    "every",
    "cron_expr",
    "agent",
    "message",
    "command",
    "script",
    "agent_sequence",
    "env",
    "persistent_session",
    "silent",
    "timezone",
    "skip_dates",
    "folder",
    "timeout",
    "timeout_secs",
    "strict_schedule",
)


#: Manifest-owned keys added after jobs already carried stamps without them,
#: with the value an entry that does not name the key means. A stamp lacking
#: one was written by a build that never sent the key, so whatever the job
#: holds for it is a person's choice; only a manifest value other than this
#: default is the author's change. Without it, the first pass after an upgrade
#: would reset a hand-set value on every app job to the default.
_LATE_KEY_DEFAULTS: dict[str, Any] = {"strict_schedule": False}


#: Hex characters kept of each per-key sha256. 12 (48 bits) tells "the author
#: changed this field" from "did not" on one job's own history, which is the
#: only comparison ever made; the whole keyed stamp (~330 chars) then fits the
#: general short-string cap with room for the keys above to grow.
_FIELD_DIGEST_CHARS = 12


def _field_digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_FIELD_DIGEST_CHARS]


def app_cron_manifest_digest(cron_def: dict[str, Any]) -> str:
    """The stamp :func:`register_app_crons_with_service` leaves on an app job.

    One ``key=digest`` (a short sha256 of the key's canonical JSON value) per
    manifest-owned key, joined with ``,`` -- so two definitions that spell the
    same thing stamp the same, and a later entry can be compared with the stamp
    FIELD BY FIELD: only the keys whose manifest value changed are re-applied,
    and a person's edit to a key the author never touched stays. Keyed rather
    than positional so a key added to ``_MANIFEST_OWNED_CRON_KEYS`` later is
    the only one an older stamp counts as changed. Computed over the
    DEFINITION, not the job: the job holds the resolved script path and the
    resolved folder id, which can differ between hosts for the same manifest.
    """
    return _join_stamp(_stamp_digests(cron_def))


def _stamp_digests(cron_def: dict[str, Any]) -> dict[str, str]:
    return {
        key: _field_digest(cron_def.get(key, _LATE_KEY_DEFAULTS.get(key)))
        for key in _MANIFEST_OWNED_CRON_KEYS
    }


def _stored_digest(stored: dict[str, str], key: str) -> str | None:
    """*key*'s digest in a readable stamp, reading a late key the stamp lacks as
    its default (see ``_LATE_KEY_DEFAULTS``)."""
    if key not in stored and key in _LATE_KEY_DEFAULTS:
        return _field_digest(_LATE_KEY_DEFAULTS[key])
    return stored.get(key)


def _join_stamp(digests: dict[str, str]) -> str:
    return ",".join(f"{key}={digests[key]}" for key in _MANIFEST_OWNED_CRON_KEYS)


def _stamp_fields(stamp: str) -> dict[str, str] | None:
    """The per-key digests a stored stamp holds, or None for a stamp this build
    cannot read (empty, or not ``key=digest`` pairs). A key this build knows
    that the stamp lacks is simply absent from the result, and so compares as
    changed; a key the stamp has that this build does not know is dropped."""
    if not stamp:
        return None
    fields: dict[str, str] = {}
    for part in stamp.split(","):
        key, sep, value = part.partition("=")
        if not sep or not value:
            return None
        if key in _MANIFEST_OWNED_CRON_KEYS:
            fields[key] = value
    return fields


def _manifest_job_fields(
    cron_def: dict[str, Any],
    *,
    command: str,
    script: str,
    folder_id: str,
    digest: str,
) -> dict[str, Any]:
    """The ``CronSDK`` keyword arguments one definition maps to, minus ``name``.

    One mapping for both the create and the re-apply path so the two cannot
    drift: a key carried on create but not on re-apply would be exactly the bug
    this exists to close. ``command``/``script`` are the vetted (and, for a
    script, resolved) values, ``folder_id`` the resolved Schedule-page folder.
    ``enabled`` is not here: see ``_MANIFEST_OWNED_CRON_KEYS``.
    """
    return dict(
        message=cron_def.get("message", ""),
        every_secs=cron_def.get("every"),  # JSON "every" -> Python "every_secs"
        cron_expr=cron_def.get("cron_expr"),
        agent=cron_def.get("agent") or "",
        command=command,
        script=script,
        agent_sequence=cron_def.get("agent_sequence") or None,
        env=cron_def.get("env") or None,
        persistent_session=bool(cron_def.get("persistent_session", False)),
        silent=bool(cron_def.get("silent", False)),
        # An empty timezone resolves to the config zone and then to UTC at fire
        # time, so a manifest that pins an hour meaningful only in one zone must
        # have it threaded here, not corrected by a second write.
        timezone=cron_def.get("timezone") or "",
        skip_dates=cron_def.get("skip_dates") or None,
        # 0 (or a def written before these keys existed) = the scheduler
        # default; CronService validates the pair.
        timeout=cron_def.get("timeout") or 0,
        timeout_secs=cron_def.get("timeout_secs") or 0,
        strict_schedule=bool(cron_def.get("strict_schedule", False)),
        folder_id=folder_id,
        app_manifest_digest=digest,
    )


#: The definition keys ``CronService.update_job`` has no branch for. They are the
#: fields the fire-time vet and a secret grant's pin bind to, so the scheduler
#: accepts them at creation only; a manifest change to one of them reaches the
#: job the way the app-kit manifest reference says it does, through a disable
#: and enable of the app (which deletes and recreates its jobs).
_CREATE_ONLY_CRON_KEYS: tuple[str, ...] = ("command", "script", "agent_sequence", "env")


#: Manifest key -> the ``CronSDK.update_job_async`` keyword(s) it is applied
#: through. ``every`` and ``cron_expr`` are one schedule, so a change to either
#: sends both (the gate reads ``cron_expr`` first and falls back to ``every_secs``).
_UPDATE_KWARGS_FOR_KEY: dict[str, tuple[str, ...]] = {
    "every": ("every_secs", "cron_expr"),
    "cron_expr": ("every_secs", "cron_expr"),
    "agent": ("agent_id",),
    "message": ("message",),
    "persistent_session": ("persistent_session",),
    "silent": ("silent",),
    "timezone": ("timezone",),
    "skip_dates": ("skip_dates",),
    "folder": ("folder_id",),
    "timeout": ("timeout",),
    "timeout_secs": ("timeout_secs",),
    "strict_schedule": ("strict_schedule",),
}


async def _resync_app_cron(
    sdk: CronSDK,
    job: Any,
    cron_def: dict[str, Any],
    *,
    digest: str,
    command: str,
    script: str,
    folder_id: str,
) -> str:
    """Bring an already-registered app job up to its changed manifest entry.

    Precedence, per field: the manifest wins for a manifest-owned field whose
    MANIFEST value changed since the stamp (the author moved it), a person's
    edit wins for every other field (the author did not touch it, so the stamp
    and the entry agree there and nothing is sent), and the job's pause state
    (``enabled``/``user_paused``/``auto_paused``) is kept always -- it is the
    person's, or the failure auto-pause's, never the manifest's. A job whose
    stamp this build cannot read (none, or not ``key=digest`` pairs) has no
    per-field history, so every manifest-owned field counts as changed once; a
    stamp that merely lacks a key this build knows counts that key alone.

    In place, through ``CronSDK.update_job_async``, so the job keeps its id, run
    history, chat tab and every setting the manifest does not own (delivery
    channel, model, chat visibility, a secret grant). The job is never removed
    and recreated: a remove-then-add would lose it when the create gate refuses
    the replacement, and would reset those settings on every pass.

    Two kinds of change cannot be applied in place and are REPORTED instead,
    every pass, until the app is disabled and enabled (which rebuilds the job
    from the manifest): a key in ``_CREATE_ONLY_CRON_KEYS``, and a ``message``
    cleared to empty (the update gate keeps the stored prompt on an empty value,
    so stamping it as applied would hide a job still running obsolete
    instructions). The stamp written holds, for each such key, the digest of
    what the job ACTUALLY carries, never the entry's -- so the drift stays
    visible: every later pass sees the stamp differ, finds the same pending
    keys and reports them again. When that is ALL that differs, nothing is
    written, so a person's edits made in the meantime are not rewritten pass
    after pass.

    Returns ``"updated"``, ``"partial"`` (updated; a change is pending a
    re-enable), ``"pending"`` (nothing new to apply; a pending change was
    reported again), ``"stale"`` (another registrar re-stamped the job between
    this pass's snapshot and its locked write; nothing written) or
    ``"vanished"`` (gone before the locked write).
    """
    fields = _manifest_job_fields(
        cron_def, command=command, script=script, folder_id=folder_id, digest=digest
    )
    wanted = {key: cron_def.get(key) for key in _MANIFEST_OWNED_CRON_KEYS}
    # The create-only keys as the JOB carries them, in manifest spelling where
    # the two coincide. ``script`` is the exception: the job holds the resolved
    # path, which no manifest spelling equals, so once it drifts it stays
    # visibly drifted until a disable/enable rebuilds the job.
    carried = {
        "command": job.command or "",
        "script": job.script or "",
        "agent_sequence": list(job.agent_sequence or []),
        "env": dict(job.env or {}),
    }
    vetted = {
        "command": command or "",
        "script": script or "",
        "agent_sequence": list(cron_def.get("agent_sequence") or []),
        "env": dict(cron_def.get("env") or {}),
    }

    stored = _stamp_fields(job.app_manifest_digest)
    current = _stamp_digests(cron_def)
    if stored is None:
        changed = list(_MANIFEST_OWNED_CRON_KEYS)
    else:
        changed = [
            key for key in _MANIFEST_OWNED_CRON_KEYS if _stored_digest(stored, key) != current[key]
        ]

    pending: list[str] = []
    applied = dict(current)
    for key in changed:
        if key in _CREATE_ONLY_CRON_KEYS and carried[key] != vetted[key]:
            pending.append(key)
            applied[key] = _field_digest(carried[key])
        elif key == "message" and not wanted["message"] and job.message:
            pending.append(key)
            applied[key] = _field_digest(job.message)
    applied_stamp = _join_stamp(applied)
    if pending and job.app_manifest_digest == applied_stamp:
        _report_pending_reenable(sdk.app_name, job.name, pending)
        return "pending"

    # The create-path fields in the update gate's spelling: ``agent`` is
    # ``agent_id`` there (the SDK's create path renames it on the way down).
    source = dict(fields)
    source["agent_id"] = source.pop("agent")
    update_kwargs: dict[str, Any] = {"app_manifest_digest": applied_stamp}
    for key in changed:
        if key in pending:
            continue
        for kwarg in _UPDATE_KWARGS_FOR_KEY.get(key, ()):
            update_kwargs[kwarg] = source[kwarg]
    # Spelled for the update gate, which differs from the create gate in three
    # more places: the create-only keys are never sent; a 0 wake budget is refused rather than read as the default, so
    # it becomes the explicit default build_job would store; and a short
    # interval is refused rather than raised to the floor, so it is clamped as
    # build_job clamps it, with cron_expr winning over every as build_job has it
    # win.
    if "timeout_secs" in update_kwargs:
        update_kwargs["timeout_secs"] = int(
            update_kwargs["timeout_secs"] or cron_seams._JOB_TIMEOUT_SECS
        )
    if "cron_expr" in update_kwargs:
        if update_kwargs["cron_expr"]:
            update_kwargs["every_secs"] = None
        elif update_kwargs["every_secs"]:
            update_kwargs["every_secs"] = max(
                int(update_kwargs["every_secs"]), cron_seams._MIN_INTERVAL_SECS
            )

    overwritten = _overwritten_fields(job, update_kwargs)
    # Read BEFORE the write: ``job`` is the store's own object, and the locked
    # update mutates it in place, so afterwards it carries the new stamp.
    had_stamp = stored is not None
    stamp_read = job.app_manifest_digest
    try:
        # Compare-and-swap on the stamp this pass computed its fields from: the
        # snapshot was taken several awaited steps ago (vetting, folder lookup),
        # and a CLI enable or another registrar may have applied a manifest to
        # this job -- and a person may have edited it -- in between. Fields
        # computed against the old stamp would then overwrite those writes, so
        # the locked update refuses, and the next registration pass recomputes
        # from the record as it then stands.
        updated = await sdk.update_job_async(
            job.id, expect_app_manifest_digest=stamp_read, **update_kwargs
        )
    except CronPendingMismatch:
        logger.info(
            "App %s: cron %r was re-stamped by another registrar during this pass; "
            "left for the next registration to reconcile",
            sdk.app_name,
            job.name,
        )
        return "stale"
    if updated is None:
        # Gone between the snapshot and the locked write (a disable raced us):
        # nothing to bring up to date, and nothing to recreate here -- the
        # registrar that removed it owns what happens next.
        return "vanished"
    if overwritten:
        # Each replaced value, old and new, so a person whose Schedule-page edit
        # the manifest just overrode can see what moved and put it back. At
        # WARNING for a job with no readable stamp: that job may carry edits
        # made under the old rule (nothing recorded which manifest it came
        # from, so the whole entry is applied once) and this line is the only
        # record of what they were.
        logger.log(
            logging.INFO if had_stamp else logging.WARNING,
            "App %s: cron %r re-applied from its manifest%s: %s",
            sdk.app_name,
            job.name,
            "" if had_stamp else " (job predates the manifest stamp)",
            "; ".join(f"{key}: {old!r} -> {new!r}" for key, old, new in overwritten),
        )
    if pending:
        _report_pending_reenable(sdk.app_name, job.name, pending)
        return "partial"
    return "updated"


#: The longest a single old or new value is quoted in the re-apply log line. A
#: ``message`` may run to thousands of characters; the line is for a person
#: looking for what moved, not a backup of the field.
_OVERWRITE_LOG_VALUE_CHARS = 120


def _overwritten_fields(job: Any, update_kwargs: dict[str, Any]) -> list[tuple[str, Any, Any]]:
    """``(field, old, new)`` for each update-gate field whose value this pass changes.

    Read against the job as it stands BEFORE the locked write, in the spelling
    the update gate stores (``every_secs``/``cron_expr`` live on the schedule;
    ``skip_dates`` None means ``[]``). The stamp itself is not a field a person
    edits and is left out.
    """
    schedule = job.schedule
    current: dict[str, Any] = {
        "message": job.message,
        "every_secs": schedule.every_secs if schedule.kind == "every" else None,
        "cron_expr": schedule.cron_expr if schedule.kind == "cron" else None,
        "agent_id": job.agent_id or "",
        "persistent_session": bool(job.persistent_session),
        "silent": bool(job.silent),
        "timezone": job.timezone or "",
        "skip_dates": list(job.skip_dates or []),
        "timeout": int(job.timeout or 0),
        "timeout_secs": int(job.timeout_secs),
        "strict_schedule": bool(job.strict_schedule),
        "folder_id": job.folder_id or "",
    }
    changed: list[tuple[str, Any, Any]] = []
    for key, old in current.items():
        if key not in update_kwargs:
            continue
        new = update_kwargs[key]
        if key == "skip_dates":
            new = list(new or [])
        elif key == "every_secs":
            # The update gate leaves an existing schedule alone on a falsy value
            # (a cron_expr entry sends None here), so None is "no change".
            if not new:
                continue
        elif key == "cron_expr" and not new:
            continue
        elif key == "message" and not new:
            # The gate skips an empty message (a command/script job has none).
            continue
        if old != new:
            changed.append((key, _clip(old), _clip(new)))
    return changed


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _OVERWRITE_LOG_VALUE_CHARS:
        return value[:_OVERWRITE_LOG_VALUE_CHARS] + "..."
    return value


def _report_pending_reenable(app_name: str, job_name: str, pending: list[str]) -> None:
    """Say, in the log and the SEL trail, that a manifest change is waiting on a re-enable.

    Emitted on EVERY registration pass while the drift stands, not once: a
    single line at one gateway start is the silent-mismatch shape this re-sync
    exists to remove.
    """
    logger.warning(
        "App %s: cron %r changed %s in its manifest; the scheduler cannot apply "
        "these to an existing job (accepted at creation only, or an empty value "
        "it does not store), so the job keeps its current value(s) until the app "
        "is disabled and enabled again",
        app_name,
        job_name,
        ", ".join(pending),
    )
    sel().log_api_access(
        caller="app_bridge",
        operation="app_cron_resync_pending_reenable",
        outcome="denied",
        resources=f"app={app_name} cron={job_name} fields={','.join(pending)}",
    )


def _admitted_app_cron_defs(app_name: str) -> tuple[list[dict[str, Any]], Path]:
    """The cron definitions the central execution decision admits for *app_name*,
    with the bundle root their ``script`` specs resolve against.

    Synchronous filesystem work (provenance lookup, the admission decision, the
    manifest or ``app-crons.json`` read), kept in one function so the registrar
    can hand all of it to a worker thread in one hop. No definitions when the
    app is denied, has no manifest, or declares no crons.
    """
    manifest, app_root = _registration_source(app_name)
    if _registration_denied(
        app_name,
        action="cron_register",
        app_root=app_root,
    ):
        return [], app_root
    if shipped_builtin_app_root(app_name) is not None:
        if manifest is None:
            return [], app_root
        defs, _ = _cron_defs_from_manifest(app_name, manifest)
        return defs, app_root
    return load_app_cron_defs(app_name), app_root


async def register_app_crons_with_service(app_name: str, cron_service: Any) -> list[str]:
    """Promote admitted app cron definitions into the running CronService.

    Third-party definitions come from the installed ``app-crons.json`` only
    after the central execution decision admits them. Shipped builtin jobs are
    rebuilt from their immutable package manifest so a mutable derivative cannot
    forge a trusted command.

    ASYNC: awaits ``CronSDK.add_job_async``, which offloads each store-lock spin
    to a worker thread — so this can be awaited directly on the gateway event
    loop (no ``asyncio.to_thread`` wrapper, no caller-side timer re-arm) without
    parking the loop. Timer arming is owned by CronService.

    Idempotent for an unchanged manifest, and the path a CHANGED manifest
    reaches an existing install by: every job carries a per-field stamp of the
    manifest entry it was built from (``app_manifest_digest``), and a job whose
    stamp equals the current entry's is left exactly as it is -- hand edits
    included. A job whose stamp differs is brought up to the manifest by
    :func:`_resync_app_cron` FIELD BY FIELD -- only the keys whose manifest
    value changed are sent, so a person's edit to a key the author never
    touched stays -- and re-stamped; a job with no readable stamp (created
    before it existed) takes the whole entry once. Its pause state is kept
    always, and a change the scheduler cannot apply in place (a create-only
    key, ``_CREATE_ONLY_CRON_KEYS``, or a message cleared to empty) waits for
    a disable/enable of the app and says so in the log and the SEL trail on
    every pass until then (the stamp records what the job carries for that
    key, not the entry, so the drift is never marked as applied).

    Returns the names of the jobs CREATED by this pass. A re-synced job is not
    new and is reported in the log and the SEL trail instead.
    """
    if cron_service is None:
        return []

    # Off-loop: this coroutine is awaited on the gateway loop (app enable, app
    # update, gateway start), and admission walks the builtin app directories,
    # reads manifests and the installed app-crons.json. Inline that parks every
    # request and the heartbeat for the scan.
    defs, app_root = await asyncio.to_thread(_admitted_app_cron_defs, app_name)
    if not defs:
        return []

    sdk = CronSDK(app_name, cron_service)
    existing_jobs = {j.name: j for j in sdk.list_jobs()}

    # circular import: mcp_cron → security → ... → hooks_integration → bridges
    from kiro_crew.mcp_cron import _vet_script_file, _vet_shell_command

    newly_registered: list[str] = []
    resynced: list[str] = []
    for d in defs:
        name = d.get("name", "")
        if not name:
            continue
        existing = existing_jobs.get(name)
        digest = app_cron_manifest_digest(d)
        if existing is not None and existing.app_manifest_digest == digest:
            # Built from the manifest entry on disk now, so nothing in it is
            # news: whatever differs on the job is a hand edit, and it stays.
            continue
        command = d.get("command") or ""
        script = d.get("script") or ""
        # Security vetting: same checks as the MCP cron_add path
        if command:
            err = _vet_shell_command(command)
            if err:
                logger.warning("App %s: cron %r command rejected: %s", app_name, name, err)
                sel().log_api_access(
                    caller="app_bridge",
                    operation="app_cron_command_vetted",
                    outcome="denied",
                    resources=f"app={app_name} cron={name}",
                    error=err,
                )
                continue
            sel().log_api_access(
                caller="app_bridge",
                operation="app_cron_command_vetted",
                outcome="allowed",
                resources=f"app={app_name} cron={name}",
            )
        if script:
            try:
                # Off-loop, for the same reason as the folder lookup below: this
                # coroutine is awaited on the gateway loop (app enable, gateway
                # start), and this step stats the script, may walk the builtin
                # manifest sources for the bundle, and reads up to
                # _MAX_SCRIPT_SCAN_BYTES (256 KiB) for the body scan. Inline that
                # parks every request and the heartbeat for its duration.
                #
                # `app_root` is bundle context the generic resolver cannot infer:
                # an app's manifest names its script RELATIVE to its own tree
                # ("job.py:run"), and this is that tree -- the immutable package
                # dir for a shipped builtin, the installed snapshot for a third
                # party, exactly as `_registration_source` chose it. Without it a
                # relative spec resolved against the gateway process's CWD, so
                # vetting looked for the file wherever the process happened to
                # start and denied the cron on every pass.
                file_path, func_name, err = await asyncio.to_thread(
                    _resolve_and_vet_app_script, script, app_root, _vet_script_file
                )
                if err:
                    logger.warning("App %s: cron %r script rejected: %s", app_name, name, err)
                    sel().log_api_access(
                        caller="app_bridge",
                        operation="app_cron_script_vetted",
                        outcome="denied",
                        resources=f"app={app_name} cron={name}",
                        error=err,
                    )
                    continue
                # Persist the RESOLVED spec, not the manifest's relative one.
                # Every later consumer re-resolves `job.script` with no app
                # context -- the fire-time governance gate, the launcher, the
                # dashboard's source endpoint -- so a relative spec on the
                # record would be re-resolved against their CWD and reproduce
                # this very bug after registration had already passed.
                script = f"{file_path}:{func_name}"
                sel().log_api_access(
                    caller="app_bridge",
                    operation="app_cron_script_vetted",
                    outcome="allowed",
                    resources=f"app={app_name} cron={name}",
                )
            except (PermissionError, FileNotFoundError, ValueError) as exc:
                logger.warning("App %s: cron %r script path rejected: %s", app_name, name, exc)
                sel().log_api_access(
                    caller="app_bridge",
                    operation="app_cron_script_vetted",
                    outcome="denied",
                    resources=f"app={app_name} cron={name}",
                    error=str(exc),
                )
                continue
        try:
            # Resolve the manifest's folder NAME against existing Schedule-page
            # folders (read-only: cron_folders.json is dashboard-owned and
            # rewritten wholesale by its state, so creating from here could be
            # silently clobbered). An unresolved folder degrades to ungrouped
            # with a logged warning rather than failing the app's enable — the
            # job matters more than its grouping, and the next enable re-files
            # it once the folder exists.
            folder_id = ""
            folder_ref = d.get("folder") or ""
            if folder_ref:
                # Off-loop: the lookup reads and JSON-parses cron_folders.json,
                # and this coroutine is awaited on the gateway loop (app enable,
                # gateway startup), so a large or slow-to-read file would stall
                # every request and the heartbeat with it.
                found = await asyncio.to_thread(lookup_cron_folder_id, folder_ref)
                folder_id = found.folder_id
                if found.error:
                    logger.warning(
                        "App %s: cron %r registered ungrouped: %s", app_name, name, found.error
                    )
            fields = _manifest_job_fields(
                d, command=command, script=script, folder_id=folder_id, digest=digest
            )
            if existing is not None:
                how = await _resync_app_cron(
                    sdk,
                    existing,
                    d,
                    digest=digest,
                    command=command,
                    script=script,
                    folder_id=folder_id,
                )
                resynced.append(f"{name} ({how})")
                sel().log_api_access(
                    caller="app_bridge",
                    operation="app_cron_resync_job",
                    outcome="allowed",
                    resources=f"app={app_name} cron={name} how={how}",
                )
                continue
            # Atomic add-if-absent: the name check and the append happen under
            # one store lock (fresh _sync first), so a CLI enable racing the
            # gateway's own registration cannot persist duplicate jobs. The
            # existing_jobs snapshot above remains only a cheap fast path to
            # skip vetting for jobs already up to date; this call is the
            # authority.
            job = await sdk.add_job_if_absent_async(
                name=name,
                enabled=bool(d.get("enabled", True)),
                **fields,
            )
            if job is None:
                # Lost the race (or already present): another registrar
                # persisted this name first. Correct outcome — not "new".
                continue
            newly_registered.append(name)
            sel().log_api_access(
                caller="app_bridge",
                operation="app_cron_add_job",
                outcome="allowed",
                resources=f"app={app_name} cron={name}",
            )
        except Exception as exc:
            logger.warning(
                "App %s: failed to register cron %r (%s): %s",
                app_name,
                name,
                type(exc).__name__,
                exc,
            )
            sel().log_api_access(
                caller="app_bridge",
                operation="app_cron_add_job",
                outcome="failed",
                resources=f"app={app_name} cron={name}",
                error=str(exc),
            )

    if newly_registered:
        logger.info(
            "App %s: registered %d cron job(s) with scheduler: %s",
            app_name,
            len(newly_registered),
            ", ".join(newly_registered),
        )
    if resynced:
        logger.info(
            "App %s: re-applied a changed manifest to %d cron job(s): %s",
            app_name,
            len(resynced),
            ", ".join(resynced),
        )
    return newly_registered


async def deregister_app_crons_reporting_failures(app_name: str, cron_service: Any) -> int:
    """Remove app-owned cron jobs, reporting EVERY failure to the caller.

    Same removal as :func:`deregister_app_crons_from_service`, which wraps this
    and is what most callers want. The difference is the disposition of an
    unexpected failure, and it exists because ``0`` is otherwise ambiguous: it is
    the honest answer for an app that owned nothing, and it is also what a write
    that never reached disk looks like from outside. A caller about to do
    something irreversible has to tell those apart, because the second case
    leaves still-ENABLED rows on disk that keep firing against an app directory
    that is gone.

    The rows cannot be re-counted to settle it either. A failing ``_save()``
    leaves the removed rows filtered out of the in-memory job list and only a
    reload restores them, so a post-hoc ``list_jobs`` reports the removal that
    did not persist.

    Returns the number of jobs removed. ``0`` from this function means the app
    owned no jobs, and nothing else.
    """
    if cron_service is None:
        return 0
    sdk = CronSDK(app_name, cron_service)
    try:
        return await sdk.remove_all_async()
    except (CronStoreBusy, CronStoreUnreadable) as exc:
        logger.warning(
            "App %s: cron cleanup could not complete (%s): %s",
            app_name,
            type(exc).__name__,
            exc,
        )
        sel().log_api_access(
            caller="app_bridge",
            operation="app_crons_deregister",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )
        raise
    except Exception as exc:
        logger.warning(
            "App %s: failed to remove crons from scheduler (%s): %s",
            app_name,
            type(exc).__name__,
            exc,
        )
        sel().log_api_access(
            caller="app_bridge",
            operation="app_crons_deregister",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )
        raise


async def deregister_app_crons_from_service(app_name: str, cron_service: Any) -> int:
    """Remove app-owned cron jobs from the running CronService.

    Mirrors :func:`register_app_crons_with_service`. Uses :class:`CronSDK`,
    which only removes jobs tagged ``created_by="app:{app_name}"`` — other
    apps' jobs are unaffected.

    ASYNC: awaits ``CronSDK.remove_all_async``, which removes all owned jobs in
    ONE atomic ``CronService.remove_jobs_by_owner`` transaction (owned set
    selected against the in-lock reloaded on-disk state, store-lock spin
    offloaded to a worker thread) — all-or-nothing, never a partial removal that
    orphans still-ENABLED app jobs, and never a cache-only snapshot that could
    miss a cross-process creation. Awaitable directly on the gateway loop.

    Idempotent — safe to call when no jobs are registered (returns ``0``).
    Returns the number of jobs removed.

    Propagates :class:`CronStoreBusy` and :class:`CronStoreUnreadable`
    (re-raised) so a cleanup that could not complete is REPORTED to the
    disable/uninstall caller as a failure rather than masked as a successful ``0``
    while owned jobs stay enabled and keep executing. The two are siblings, not
    subclasses, so each needs naming: an unreadable store degrades to an empty job
    list, which is indistinguishable HERE from an app that owned nothing.

    An UNEXPECTED failure is reported as ``0``, which a caller must not read as
    "the app owned nothing" — the logging and the audit entry above are where that
    case is visible. A caller that has to tell the two apart, because what it does
    next cannot be undone, calls
    :func:`deregister_app_crons_reporting_failures` instead.
    """
    try:
        return await deregister_app_crons_reporting_failures(app_name, cron_service)
    except (CronStoreBusy, CronStoreUnreadable):
        raise
    except Exception:
        # Logged and audited by the call above; this frame only chooses the
        # disposition its own callers are written against.
        return 0


async def disarm_app_crons_for_execution(
    app_name: str,
    cron_service: Any,
) -> int:
    """Atomically remove app-owned jobs with best-effort audit logging."""
    if cron_service is None:
        return 0
    removed = await cron_service.remove_jobs_by_owner(f"app:{app_name}")
    try:
        sel().log_api_access(
            caller="app_bridge",
            operation="app_crons_execution_disarm",
            outcome="completed",
            resources=f"app={app_name} removed={len(removed)}",
        )
    except Exception:  # noqa: BLE001 - cleanup verdict must survive audit failure
        logger.debug("app cron execution-disarm audit failed", exc_info=True)
    return len(removed)


async def reconcile_app_crons_for_execution(cron_service: Any) -> list[str]:
    """Disarm persisted app jobs before the gateway starts cron timers.

    ``CronService.create`` has loaded the durable store but has not armed its
    timer yet when this runs. Any cleanup failure propagates so the caller can
    leave the scheduler stopped rather than execute a denied job.
    """
    if cron_service is None:
        return []

    # list_apps() walks the apps dir (two file reads per app), and this runs on
    # the gateway boot path — off the loop.
    app_infos = await asyncio.to_thread(list_apps)
    installed_names = {app_name for app_info in app_infos if (app_name := app_info.get("name", ""))}
    cron_owner_names: set[str] = set()
    for job in cron_service.list_jobs(include_disabled=True):
        owner = str(getattr(job, "created_by", ""))
        if owner.startswith("app:") and owner != "app:":
            cron_owner_names.add(owner.removeprefix("app:"))

    disarmed: list[str] = []
    for app_name in sorted(cron_owner_names - installed_names):
        reason = "orphaned app cron owner has no installed app"
        logger.warning(
            "App %s: denying persisted cron restore (cron_boot_restore): %s",
            app_name,
            reason,
        )
        try:
            sel().log_api_access(
                caller="app_bridge",
                operation="app_execution_admission",
                outcome="denied",
                resources=(f"app={app_name} action=cron_boot_restore " "provenance=unverified"),
                error=reason,
            )
        except Exception:  # noqa: BLE001 - denial must survive audit unavailability
            logger.debug("app execution denial audit failed", exc_info=True)

        removed = await disarm_app_crons_for_execution(app_name, cron_service)
        if removed:
            disarmed.append(app_name)
            logger.warning(
                "Boot: disarmed %d persisted cron(s) for orphaned app %s",
                removed,
                app_name,
            )

    for app_info in app_infos:
        app_name = app_info.get("name", "")
        if not app_name:
            continue

        should_disarm = not bool(app_info.get("enabled"))
        if not should_disarm:
            _, app_root = _registration_source(app_name)
            should_disarm = bool(
                _registration_denied(
                    app_name,
                    action="cron_boot_restore",
                    app_root=app_root,
                )
            )
        if not should_disarm:
            continue

        removed = await disarm_app_crons_for_execution(app_name, cron_service)
        if removed:
            disarmed.append(app_name)
            logger.warning(
                "Boot: disarmed %d persisted cron(s) for inactive app %s",
                removed,
                app_name,
            )
    return disarmed
