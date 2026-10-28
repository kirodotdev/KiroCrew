"""Session environments that keep one persistent VM per session (Sprites, boxd).

A sprite or a boxd machine keeps its disk when idle, so a session keeps one for
its life and its files survive between turns and gateway restarts. An archived
session's VM is kept for ``ARCHIVED_RETENTION_DAYS`` (seven days) and then
deleted; reopening the session before then keeps it.

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
    #: The vault secret the profile named at create time (Sprites), so the row
    #: can still be deleted after its profile is removed.
    token_secret: str = ""
    #: A one-way fingerprint of the credential the resource was created with
    #: (Sprites: a truncated SHA-256 of the API token, never the token). A lookup
    #: miss under a different credential is another account, not a deletion.
    credential_id: str = ""
    #: The machine's login home, read at start when the profile names no
    #: working folder; the session's folder then.
    home: str = ""

    @classmethod
    def from_dict(cls, value: Any) -> "SessionResource":
        """Read only the declared fields, so a key a newer build adds is dropped.

        A missing field without a default still raises (``TypeError``), which the
        registry reads as corruption.
        """
        if not isinstance(value, dict):
            raise TypeError("a resource row must be an object")
        return cls(**{k: value[k] for k in cls.__dataclass_fields__ if k in value})


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
            return {str(k): SessionResource.from_dict(v) for k, v in rows.items()}
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

    def set_credential_id(self, session_key: str, name: str, credential_id: str) -> None:
        def mutate(rows: dict[str, SessionResource]) -> None:
            current = rows.get(session_key)
            if current is not None and current.name == name:
                rows[session_key] = replace(current, credential_id=credential_id)

        self._locked(mutate)

    def set_home(self, session_key: str, name: str, home: str) -> None:
        def mutate(rows: dict[str, SessionResource]) -> None:
            current = rows.get(session_key)
            if current is not None and current.name == name:
                rows[session_key] = replace(current, home=home)

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
    #: Whether every remote call needs the profile's stored credential (Sprites'
    #: token). boxd uses the gateway's own CLI login, so a removed boxd profile
    #: still lets its machine be idled and deleted.
    needs_credential = False

    def __init__(self, config: Any, data_home: Path) -> None:
        self._config = config
        self.data_home = Path(data_home)
        self.registry = SessionResourceRegistry(self.data_home / self.registry_file, self.label)

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

    def bound_profile(self, row: SessionResource) -> dict[str, Any]:
        """The row's profile, refused when it now names another token key.

        A VM is reached only with the key it was created with: under another key
        (another organization) its name would answer 404, which must never read
        as "deleted outside Kiro Crew" and replace the session's files.
        """
        profile = self.profile(row.profile_id)
        current = str(profile.get("token_secret") or "")
        if row.token_secret and current != row.token_secret:
            raise SessionEnvironmentUnavailable(
                f"this session's {self.label} machine was created with the token in "
                f"{row.token_secret!r}; set that key on its profile again to reopen it"
            )
        return profile

    def cleanup_profile(self, row: SessionResource) -> Optional[dict[str, Any]]:
        """The profile archive and deletion use: always the row's own token key."""
        try:
            profile: Optional[dict[str, Any]] = dict(self.profile(row.profile_id))
        except SessionEnvironmentUnavailable:
            removed = bool(row.token_secret) or not self.needs_credential
            profile = {"id": row.profile_id} if removed else None
        if profile is not None and row.token_secret:
            profile["token_secret"] = row.token_secret
        return profile

    async def _bind_live(
        self, row: SessionResource, record: dict[str, Any], used: str
    ) -> SessionResource:
        """Act on a live VM only as this row's: the one place a lookup is trusted.

        Refuses a VM under the recorded name with another provider id. A row
        recovered from an interrupted create (no id saved yet) is pinned to the
        VM that answers now, so a later same-name replacement is refused rather
        than archived or deleted as this session's. Remembers the credential
        that answered (a token rotated in the same account).
        """
        self.check_identity(row, record)
        resource_id = self.record_id(record)
        if resource_id and not row.resource_id:
            await asyncio.to_thread(
                self.registry.set_resource_id, row.session_key, row.name, resource_id
            )
            row = replace(row, resource_id=resource_id)
        if used and used != row.credential_id:
            await asyncio.to_thread(
                self.registry.set_credential_id, row.session_key, row.name, used
            )
            row = replace(row, credential_id=used)
        return row

    def _refuse_if_credential_changed(self, row: SessionResource, used: str) -> None:
        """A lookup miss under another credential is another account: keep the row."""
        if row.credential_id and used and used != row.credential_id:
            raise SessionEnvironmentUnavailable(self.credential_changed(row))

    def credential_changed(self, row: SessionResource) -> str:
        """What a miss under another account tells the person (and what to do)."""
        return (
            f"this session's {self.label} machine is not visible with the token now saved "
            f"as {row.token_secret!r}, which is not the one it was created with; nothing "
            "was deleted. Save that token again to reopen the session"
        )

    def retention_days(self) -> int:
        from kiro_crew.config.session_environments import ARCHIVED_RETENTION_DAYS

        return ARCHIVED_RETENTION_DAYS

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
    ) -> tuple[Optional[dict[str, Any]], str]:
        """The live record of ``name`` (None when it does not exist), with the
        fingerprint of the credential that made this very request ("" for a
        provider with none of its own). Wakes nothing.

        The fingerprint comes from the request itself, never a second read, so a
        token saved while the lookup is in flight cannot be credited with its
        answer.
        """
        raise NotImplementedError

    async def remote_credential(self, profile: dict[str, Any]) -> str:
        """The fingerprint of the credential a create would use now (as
        :meth:`remote_get`; "" for a provider with none of its own)."""
        return ""

    async def remote_create(
        self, profile: dict[str, Any], name: str, *, used: str
    ) -> dict[str, Any]:
        """Create ``name`` from the profile and return the provider's record of it.

        ``used`` is the fingerprint :meth:`remote_credential` returned, already
        saved on the row: the create must go out under that same credential and
        refuse when another one is current, so a row whose create was cut short
        always names the account its machine lives in.
        """
        raise NotImplementedError

    async def remote_delete(
        self, profile: Optional[dict[str, Any]], name: str, *, used: str
    ) -> None:
        """Delete ``name``; one already gone is not an error. Raises when it may remain.

        ``used`` is the fingerprint :meth:`remote_get` returned for the identity
        check: the delete must go out under that same credential, so one saved
        in between can never answer it (and its miss never drops the row).
        """
        raise NotImplementedError

    async def remote_kept(self, profile: dict[str, Any], name: str) -> None:
        """A reopened archived session keeps ``name``: undo :meth:`remote_archived`."""

    async def remote_archived(self, profile: dict[str, Any], name: str, days: int) -> None:
        """An archived session's ``name`` is kept ``days`` days: let it go idle."""

    async def shell(self, profile: dict[str, Any], row: SessionResource) -> Any:
        """A shell on ``row``'s VM, only under the credential that passed its
        identity check (``row.credential_id``, set by :meth:`_bind_live`)."""
        raise NotImplementedError

    def launch(self, binding_id: str, shell: Any) -> "RemoteWorkerLaunch":
        from kiro_crew.coder.remote_worker import RemoteWorkerLaunch

        return RemoteWorkerLaunch(binding_id, shell)

    # -- identity ---------------------------------------------------------------------
    @staticmethod
    def record_id(record: Optional[dict[str, Any]]) -> str:
        value = record.get("id") if isinstance(record, dict) else None
        return str(value) if isinstance(value, (str, int)) and str(value) else ""

    @staticmethod
    def record_running(record: Optional[dict[str, Any]]) -> bool:
        """Whether the live record says the VM is on compute now.

        Both APIs report ``running`` for that; anything else (a cold or warm
        sprite, a hibernated boxd machine, no status at all) would be woken by
        an exec, so a caller that must not wake one treats it as stopped.
        """
        return isinstance(record, dict) and record.get("status") == "running"

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
        self.profile(profile_id)
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is None:
            return None
        record, used = await self.remote_get(self.bound_profile(row), row.name)
        if record is not None:
            row = await self._bind_live(row, record, used)
        return row, record

    # -- prepare ----------------------------------------------------------------------
    async def prepare(
        self, session_key: str, profile_id: str, *, progress_key: str | None = None
    ) -> "PreparedRemoteSession":
        from kiro_crew.coder.retention import session_lifecycle

        lifecycle = session_lifecycle(session_key)
        # A reopen supersedes an archive queued before it, and waits for one
        # already running so its stamp is never written after this clears it.
        lifecycle.generation += 1
        async with lifecycle.lock:
            return await self._prepare(session_key, profile_id, progress_key or session_key)

    async def _prepare(
        self, session_key: str, profile_id: str, progress_key: str
    ) -> "PreparedRemoteSession":
        from kiro_crew.coder.session_launch import PreparedRemoteSession

        profile = self.profile(profile_id)
        await self.check_ready(profile)
        workdir = str(profile.get("workdir") or "")
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is not None:
            # Refused, never replaced, when the profile now names another token key.
            bound = self.bound_profile(row)
            record, used = await self.remote_get(bound, row.name)
            if record is None:
                # Under another account's token a miss proves nothing: keep the row.
                self._refuse_if_credential_changed(row, used)
                # Deleted outside Kiro Crew: forget it and start on a fresh one.
                await asyncio.to_thread(self.registry.remove, session_key, row.name)
                row = None
            else:
                row = await self._bind_live(row, record, used)
                if row.deletion_due_at:
                    # Reopened before its retention ran out: keep it, files intact.
                    await self.remote_kept(profile, row.name)
                    await asyncio.to_thread(self.registry.set_deletion, session_key, "")
        created = row is None
        if row is None:
            row = await self._create(session_key, profile, progress_key)
        shell = await self.shell(profile, row)
        try:
            if workdir:
                await shell.run(f"mkdir -p {shlex.quote(workdir)}")
            else:
                workdir = await self._login_home(shell, row)
        except SessionEnvironmentUnavailable:
            raise
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

    async def _login_home(self, shell: Any, row: SessionResource) -> str:
        """The machine's own login home, for a profile that names no working folder.

        Read from the machine at each start and recorded on the row, so Files
        and the terminal open the same folder without an exec of their own.
        """
        home = (await shell.run('printf %s "$HOME"')).decode("utf-8", "replace").strip()
        if not home.startswith("/") or any(ord(c) < 32 for c in home):
            raise SessionEnvironmentUnavailable(
                f"the {self.label} machine reported no home folder; set a working "
                "folder on its profile"
            )
        if row.home != home:
            await asyncio.to_thread(self.registry.set_home, row.session_key, row.name, home)
        return home

    def session_workdir(self, profile: dict[str, Any], row: SessionResource) -> str:
        """The folder a session runs in: the profile's, else the recorded login home."""
        return str(profile.get("workdir") or "") or row.home

    async def _create(
        self, session_key: str, profile: dict[str, Any], progress_key: str
    ) -> SessionResource:
        report_start_stage(progress_key, "provisioning")
        # The account is recorded with the reservation: a create cut short
        # before its record is bound still names where its machine lives, so a
        # later miss under another account keeps the row instead of replacing it.
        used = await self.remote_credential(profile)
        row = SessionResource(
            session_key,
            str(profile["id"]),
            self.mint_name(),
            "",
            stamp(now_utc()),
            token_secret=str(profile.get("token_secret") or ""),
            credential_id=used,
        )
        # Recorded before the create, so an archive (or a failure below) can delete it.
        await asyncio.to_thread(self.registry.put, row)
        try:
            record = await self.remote_create(profile, row.name, used=used)
        except BaseException:
            try:
                await self._delete(row)
            except Exception:  # noqa: BLE001 -- keep the row for a later prepare or archive
                logger.info("%s create failed and its cleanup did too", self.label)
            raise
        # Its id and the token that made it, so a later same-name VM or a miss
        # under another account's token is never taken for this session's.
        return await self._bind_live(row, record, used)

    # -- archive and retention --------------------------------------------------------
    async def archive_session(self, session_key: str) -> bool:
        """Schedule an archived session's VM for deletion after the retention.

        The caller holds the session's lifecycle lock. False when it has none.
        """
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is None:
            return False
        days = self.retention_days()
        due = stamp(now_utc() + timedelta(days=days))
        await asyncio.to_thread(self.registry.set_deletion, session_key, due)
        try:
            profile = self.cleanup_profile(row)
            if profile is None:
                raise SessionEnvironmentUnavailable(
                    f"this session's {self.label} profile is no longer configured"
                )
            record, used = await self.remote_get(profile, row.name)
            if record is not None:
                row = await self._bind_live(row, record, used)
                await self.remote_archived(profile, row.name, days)
        except SessionEnvironmentUnavailable as exc:
            # The stamp stands; the sweep deletes it when due.
            logger.info("Archived session %s machine: could not idle it (%s)", self.label, exc)
        return True

    async def reopen_session(self, session_key: str) -> bool:
        """Keep a reopened archived session's VM: undo :meth:`archive_session`.

        Nothing is created or woken. The caller holds the session's lifecycle
        lock. The live VM is looked up and identity-checked first, as archive
        and the sweep do: a same-name replacement is never touched. Raises
        :class:`SessionEnvironmentUnavailable` when a deadline still stands
        (the provider-side undo failed, or a miss under another account), so a
        history resume refuses instead of publishing a doomed tab. False when
        nothing was pending.
        """
        row = await asyncio.to_thread(self.registry.get, session_key)
        if row is None or not row.deletion_due_at:
            return False
        profile = self.cleanup_profile(row)
        if profile is not None:
            record, used = await self.remote_get(profile, row.name)
            if record is None:
                # Gone, unless it is only hidden from another account's credential.
                self._refuse_if_credential_changed(row, used)
            else:
                try:
                    row = await self._bind_live(row, record, used)
                except SessionEnvironmentUnavailable as exc:
                    # Another machine took the name: ours is gone, and that one
                    # is not this session's to keep. The next start says so.
                    logger.info("Reopened session %s machine: %s", self.label, exc)
                    return False
                try:
                    await self.remote_kept(profile, row.name)
                except Exception as exc:  # noqa: BLE001 -- refused; the resume is retried
                    logger.info(
                        "Reopened session %s machine: could not cancel its deletion (%s)",
                        self.label,
                        exc,
                    )
                    raise SessionEnvironmentUnavailable(
                        f"this session's {self.label} machine is still scheduled for "
                        "deletion and that could not be cancelled; try again in a minute"
                    ) from exc
        await asyncio.to_thread(self.registry.set_deletion, session_key, "")
        return True

    async def _delete(self, row: SessionResource) -> None:
        # Always the key the VM was created with, even when the profile changed.
        profile = self.cleanup_profile(row)
        record, used = await self.remote_get(profile, row.name)
        if record is not None:
            self.check_identity(row, record)
            await self.remote_delete(profile, row.name, used=used)
        else:
            # A miss under another account's token is not a deletion: keep the row.
            self._refuse_if_credential_changed(row, used)
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
