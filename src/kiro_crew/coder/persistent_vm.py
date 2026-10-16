"""Session environments that keep one persistent VM per session (Sprites, boxd).

A sprite or a boxd machine keeps its disk when idle, so a session keeps one for
its life and its files survive between turns and gateway restarts. An archived
session's VM is kept for ``archived_retention_days`` and then deleted; reopening
the session before then keeps it.

Two pieces, shared by both providers:

* :class:`SessionResourceRegistry` is the only record that a name is this
  gateway's. A provider acts on a VM only through its row, and a row is written
  BEFORE the create call, so a failed or interrupted create can still be
  deleted. Written atomically (a unique temp file, then a rename) under a
  blocking cross-process lock, so every call runs on a worker thread.
* :class:`PersistentVmEnvironments` prepares, archives and sweeps, serialized
  per session by the same lifecycle lock and generation the Coder provider uses
  (``retention.session_lifecycle``): a reopen supersedes a queued archive, and a
  sweep re-reads the row under that lock before it deletes anything.

A VM deleted outside Kiro Crew is replaced by a fresh one under a new minted
name, never looked up by any other name. One that answers under the recorded
name with a different provider id is refused, never adopted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from kiro_crew.atomic_write import atomic_write
from kiro_crew.platform_compat import file_lock
from kiro_crew.session_environment import SessionEnvironmentUnavailable, report_start_stage

if TYPE_CHECKING:
    from kiro_crew.coder.remote_worker import RemoteWorkerLaunch
    from kiro_crew.coder.session_launch import PreparedRemoteSession

logger = logging.getLogger(__name__)

_VERSION = 1

# Providers are rebuilt each sweep; keep only the last failure of each due row.
_last_delete_error: dict[tuple[str, str], str] = {}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def stamp(moment: datetime) -> str:
    """A UTC timestamp that sorts as text."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class SessionResource:
    session_key: str
    profile_id: str
    #: The name the gateway minted for the resource.
    name: str
    #: The provider's own id for it, when the create call returned one. A live
    #: resource under ``name`` with a different id is not this session's.
    resource_id: str = ""
    created_at: str = ""
    #: Set when the session is archived: when the resource is deleted.
    deletion_due_at: str = ""


class SessionResourceRegistry:
    """``session key -> SessionResource``, one file per provider."""

    def __init__(self, path: Path, label: str) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")
        #: Names the provider in errors, e.g. "Sprites".
        self.label = label

    def _read(self) -> dict[str, SessionResource]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except OSError as exc:
            raise SessionEnvironmentUnavailable(
                f"the {self.label} session registry is unreadable"
            ) from exc
        try:
            raw = json.loads(text)
            if not isinstance(raw, dict) or raw.get("version") != _VERSION:
                raise ValueError("unknown registry version")
            rows = raw.get("resources")
            if not isinstance(rows, dict):
                raise ValueError("resources must be an object")
            return {str(k): SessionResource(**v) for k, v in rows.items()}
        except (ValueError, TypeError) as exc:
            # Fail closed: a resource the gateway cannot read is never treated as
            # absent, which would let a new one be created beside it.
            raise SessionEnvironmentUnavailable(
                f"the {self.label} session registry is unreadable"
            ) from exc

    def _write(self, rows: dict[str, SessionResource]) -> None:
        payload = {
            "version": _VERSION,
            "resources": {k: asdict(v) for k, v in sorted(rows.items())},
        }
        atomic_write(
            self.path, json.dumps(payload, indent=2) + "\n", fsync=True, restrict_to_owner=True
        )

    def _locked(self, mutate: Callable[[dict[str, SessionResource]], Any]) -> Any:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as handle:
            with file_lock(handle.fileno(), exclusive=True, required=True):
                rows = self._read()
                before = dict(rows)
                result = mutate(rows)
                if rows != before:
                    self._write(rows)
                return result

    def get(self, session_key: str) -> Optional[SessionResource]:
        return self._read().get(session_key)

    def all(self) -> list[SessionResource]:
        return list(self._read().values())

    def put(self, row: SessionResource) -> None:
        self._locked(lambda rows: rows.__setitem__(row.session_key, row))

    def remove(self, session_key: str, name: str) -> bool:
        """Drop the row only while it still names ``name`` (a newer one stays)."""

        def mutate(rows: dict[str, SessionResource]) -> bool:
            current = rows.get(session_key)
            if current is None or current.name != name:
                return False
            del rows[session_key]
            return True

        return bool(self._locked(mutate))

    def set_resource_id(self, session_key: str, name: str, resource_id: str) -> None:
        def mutate(rows: dict[str, SessionResource]) -> None:
            current = rows.get(session_key)
            if current is not None and current.name == name:
                rows[session_key] = replace(current, resource_id=resource_id)

        self._locked(mutate)

    def set_deletion(self, session_key: str, due: str) -> Optional[SessionResource]:
        """Stamp (or with ``""`` clear) the row's deletion time; the updated row."""

        def mutate(rows: dict[str, SessionResource]) -> Optional[SessionResource]:
            current = rows.get(session_key)
            if current is None:
                return None
            rows[session_key] = replace(current, deletion_due_at=due)
            return rows[session_key]

        result: Optional[SessionResource] = self._locked(mutate)
        return result

    def due(self, moment: datetime) -> list[SessionResource]:
        cutoff = stamp(moment)
        return [
            r for r in self._read().values() if r.deletion_due_at and r.deletion_due_at <= cutoff
        ]


class PersistentVmEnvironments:
    """Create, reuse, archive and delete one VM per session for one provider.

    A provider subclass names its profile list and implements the remote calls;
    everything that decides WHEN a VM is created, kept or deleted lives here.
    """

    #: ``SessionEnvironmentsConfig`` attribute holding this provider's profiles.
    profiles_key = ""
    #: Display name, for errors and the registry.
    label = ""
    #: Prefix of every name this gateway mints, followed by 16 random hex digits.
    name_prefix = ""
    #: Prefix of the guest worker binding id, followed by the name's hex digits.
    binding_prefix = ""
    #: Registry file, relative to the data home.
    registry_file = ""

    def __init__(self, config: Any, data_home: Path) -> None:
        self._config = config
        self.data_home = Path(data_home)
        self.registry = SessionResourceRegistry(self.data_home / self.registry_file, self.label)
        self.worker_secrets_dir = self.data_home / "remote-session-secrets"

    # -- configuration --------------------------------------------------------------
    def profiles(self) -> list[dict[str, Any]]:
        return [
            p for p in (getattr(self._config, self.profiles_key, None) or []) if isinstance(p, dict)
        ]

    def profile(self, profile_id: str) -> dict[str, Any]:
        for p in self.profiles():
            if p.get("id") == profile_id:
                return p
        raise SessionEnvironmentUnavailable(
            f"this session's {self.label} profile is no longer configured"
        )

    def retention_days(self) -> int:
        from kiro_crew.coder.retention import retention_days

        return retention_days(self._config)

    def mint_name(self) -> str:
        return self.name_prefix + os.urandom(8).hex()

    def binding_id(self, row: SessionResource) -> str:
        return self.binding_prefix + row.name[len(self.name_prefix) :]

    # -- the provider's remote calls ------------------------------------------------
    async def check_ready(self, profile: dict[str, Any]) -> None:
        """Raise when the gateway cannot reach the provider at all (no token, no CLI).

        Runs before a row is reserved, so a gateway that cannot reach the
        provider leaves nothing behind.
        """

    async def remote_get(
        self, profile: Optional[dict[str, Any]], name: str
    ) -> Optional[dict[str, Any]]:
        """The live record of ``name``, or None when it does not exist. Wakes nothing."""
        raise NotImplementedError

    async def remote_create(self, profile: dict[str, Any], name: str) -> dict[str, Any]:
        """Create ``name`` from the profile; the provider's record of it."""
        raise NotImplementedError

    async def remote_delete(self, profile: Optional[dict[str, Any]], name: str) -> None:
        """Delete ``name``; one already gone is not an error. Raises when it may remain."""
        raise NotImplementedError

    async def remote_kept(self, profile: dict[str, Any], name: str) -> None:
        """A reopened archived session keeps ``name``: undo :meth:`remote_archived`."""

    async def remote_archived(self, profile: dict[str, Any], name: str, days: int) -> None:
        """An archived session's ``name`` is kept ``days`` days: let it go idle."""

    async def shell(self, profile: dict[str, Any], name: str) -> Any:
        raise NotImplementedError

    def launch(self, binding_id: str, shell: Any) -> "RemoteWorkerLaunch":
        from kiro_crew.coder.remote_worker import RemoteWorkerLaunch

        return RemoteWorkerLaunch(binding_id, shell, self.worker_secrets_dir)

    # -- identity ---------------------------------------------------------------------
    @staticmethod
    def record_id(record: Optional[dict[str, Any]]) -> str:
        value = record.get("id") if isinstance(record, dict) else None
        return str(value) if isinstance(value, (str, int)) and str(value) else ""

    def check_identity(self, row: SessionResource, record: dict[str, Any]) -> None:
        """Refuse a live VM under the recorded name that is not the one created."""
        if row.resource_id and self.record_id(record) != row.resource_id:
            raise SessionEnvironmentUnavailable(
                f"this session's {self.label} machine now names another machine"
            )

    async def existing(
        self, session_key: str, profile_id: str
    ) -> Optional[tuple[SessionResource, Optional[dict[str, Any]]]]:
        """The session's row and the VM's live record (None when it is gone).

        Nothing is created or woken. The live record is checked against the
        row's identity, so a caller may act on the VM.
        """
        profile = self.profile(profile_id)
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is None:
            return None
        record = await self.remote_get(profile, row.name)
        if record is not None:
            self.check_identity(row, record)
        return row, record

    # -- prepare ----------------------------------------------------------------------
    async def prepare(self, session_key: str, profile_id: str) -> "PreparedRemoteSession":
        from kiro_crew.coder.retention import session_lifecycle

        lifecycle = session_lifecycle(session_key)
        # A reopen supersedes an archive queued before it, and waits for one
        # already running so its stamp is never written after this clears it.
        lifecycle.generation += 1
        async with lifecycle.lock:
            return await self._prepare(session_key, profile_id)

    async def _prepare(self, session_key: str, profile_id: str) -> "PreparedRemoteSession":
        from kiro_crew.coder.session_launch import PreparedRemoteSession

        profile = self.profile(profile_id)
        await self.check_ready(profile)
        workdir = str(profile.get("workdir") or "")
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is not None:
            record = await self.remote_get(profile, row.name)
            if record is None:
                # Deleted outside Kiro Crew: forget it and start on a fresh one.
                await asyncio.to_thread(self.registry.remove, session_key, row.name)
                row = None
            else:
                self.check_identity(row, record)
                if row.deletion_due_at:
                    # Reopened before its retention ran out: keep it, files intact.
                    await self.remote_kept(profile, row.name)
                    await asyncio.to_thread(self.registry.set_deletion, session_key, "")
        created = row is None
        if row is None:
            row = await self._create(session_key, profile)
        shell = await self.shell(profile, row.name)
        if workdir:
            try:
                await shell.run(f"mkdir -p {shlex.quote(workdir)}")
            except Exception as exc:  # noqa: BLE001 -- surfaced as an unavailable session
                raise SessionEnvironmentUnavailable(
                    f"the {self.label} machine did not answer: {exc}"
                ) from exc
        return PreparedRemoteSession(
            self.launch(self.binding_id(row), shell),
            workdir,
            row.name,
            str(profile["id"]),
            str(profile.get("name") or profile["id"]),
            created=created,
        )

    async def _create(self, session_key: str, profile: dict[str, Any]) -> SessionResource:
        report_start_stage(session_key, "provisioning")
        row = SessionResource(
            session_key, str(profile["id"]), self.mint_name(), "", stamp(now_utc())
        )
        # Recorded before the create, so an archive (or a failure below) can delete it.
        await asyncio.to_thread(self.registry.put, row)
        try:
            record = await self.remote_create(profile, row.name)
        except BaseException:
            try:
                await self._delete(row)
            except Exception:  # noqa: BLE001 -- keep the row for a later prepare or archive
                logger.info("%s create failed and its cleanup did too", self.label)
            raise
        resource_id = self.record_id(record)
        if resource_id:
            await asyncio.to_thread(
                self.registry.set_resource_id, session_key, row.name, resource_id
            )
            row = replace(row, resource_id=resource_id)
        return row

    # -- archive and retention --------------------------------------------------------
    async def archive_session(self, session_key: str) -> bool:
        """Schedule an archived session's VM for deletion; 0 days deletes it now.

        The caller holds the session's lifecycle lock. False when it has none.
        """
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is None:
            return False
        days = self.retention_days()
        due = stamp(now_utc() + timedelta(days=days))
        await asyncio.to_thread(self.registry.set_deletion, session_key, due)
        try:
            profile = self.profile(row.profile_id)
            record = await self.remote_get(profile, row.name)
            if record is not None:
                self.check_identity(row, record)
                await self.remote_archived(profile, row.name, days)
        except SessionEnvironmentUnavailable as exc:
            # The stamp stands; the sweep can retry even if immediate deletion fails.
            logger.info("Archived session %s machine: could not idle it (%s)", self.label, exc)
        if days <= 0:
            await self._delete(row)
        return True

    async def _delete(self, row: SessionResource) -> None:
        try:
            profile: Optional[dict[str, Any]] = self.profile(row.profile_id)
        except SessionEnvironmentUnavailable:
            profile = None
        record = await self.remote_get(profile, row.name)
        if record is not None:
            self.check_identity(row, record)
            await self.remote_delete(profile, row.name)
        await asyncio.to_thread(self.registry.remove, row.session_key, row.name)

    async def sweep_due(self, *, now: Optional[datetime] = None) -> int:
        """Delete every archived session's VM whose retention ran out.

        Registry-only while nothing is due, so a gateway with no such VM makes no
        remote call. Each deletion re-reads its row under the session's lifecycle
        lock, so a session reopened since the scan keeps its VM.
        """
        from kiro_crew.coder.retention import session_lifecycle

        moment = now or now_utc()
        deleted = 0
        candidates = await asyncio.to_thread(self.registry.due, moment)
        due_names = {row.name for row in candidates}
        for key in list(_last_delete_error):
            if key[0] == self.label and key[1] not in due_names:
                del _last_delete_error[key]
        for candidate in candidates:
            error_key = (self.label, candidate.name)
            lifecycle = session_lifecycle(candidate.session_key)
            async with lifecycle.lock:
                row = await asyncio.to_thread(self.registry.get, candidate.session_key)
                if (
                    row is None
                    or row.name != candidate.name
                    or not row.deletion_due_at
                    or row.deletion_due_at > stamp(moment)
                ):
                    _last_delete_error.pop(error_key, None)
                    continue  # reopened, or replaced, in the meantime
                try:
                    await self._delete(row)
                except SessionEnvironmentUnavailable as exc:
                    message = str(exc)
                    if _last_delete_error.get(error_key) != message:
                        logger.warning(
                            "Archived session %s machine %s: delete failed (%s); retrying later",
                            self.label,
                            row.name,
                            message,
                        )
                    _last_delete_error[error_key] = message
                    continue
                _last_delete_error.pop(error_key, None)
            deleted += 1
        return deleted
