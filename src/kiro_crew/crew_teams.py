"""Crewmate teams: ``$KIROCREW_HOME/crew-teams/teams.json``.

Named ``crew_teams`` because ``kiro_crew.teams`` is the Microsoft Teams channel
package; a same-named module would be shadowed by it.

A team is a name plus an ordered list of crewmates (exact crew names). It is
the manager's grouping of the roster, nothing more: no memory, no chat, no
prompt of its own. The Crewmates page groups the roster by team and opens a
team view for each; everything in that view is derived from the members'
existing threads, activity and presence.

Every mutation runs read -> mutate -> rewrite under TWO locks: a process-wide
``threading.Lock`` (``_WRITE_LOCK``) and, inside it, an advisory file lock on
``crew-teams/.lock`` (``platform_compat.file_lock``). The gateway's writers (the
owner routes, the crew create/delete/package-sync hooks) run off-loop in worker
threads; ``kirocrew agent create`` / ``delete`` write from ANOTHER process. Two
interleaved rewrites of one document would otherwise have the second silently
drop the first's change, and only the file lock reaches the CLI. Writers hold
the lock for a sub-second read plus an atomic rename, so the default ceiling
applies; a contended acquire past it fails closed (``OSError``, answered as
``teams_write_failed``) rather than writing unserialized.

Two invariants the store enforces so no UI has to:

* **A crewmate is on at most one team.** Assigning a crewmate to a team removes
  it from whichever team held it, in the same write, so two readers can never
  see it in two groups.
* **The file is one document.** Every mutation rewrites the whole list
  atomically (unique temp file + rename), so a torn record is never observable.

The file lives in its own data-home directory, ``crew-teams/``, and NOT under
``trust/``: ``trust`` is a declared sandbox READ-WRITE exception (in-sandbox code
appends to the audit log there), so a record under it stays writable by a
sandboxed command that builds the path at runtime -- the ``crew-panels`` lesson
in ``sandbox.py``. ``crew-teams`` is instead masked from every sandboxed process
(``sandbox._CREW_HIDDEN_LEAVES``, pre-created before each spawn so the mask is
never vacuous) and fenced from agent file tools (``security._CREW_SECRET_LEAVES``).
Two trusted writers open it, both owner-invoked: the gateway on a dashboard
action (the ``/api/teams`` routes, the crew create, delete and package-sync
hooks) and ``kirocrew agent create`` / ``delete`` from the operator's shell. A crewmate's
own tools therefore cannot re-team itself or its siblings, and the team view is
a human surface built on human choices.

Member NAMES, not slugs, are stored: slugification is lossy (two names can
share one slug), and the roster is keyed and selected by exact name.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import (
    atomic_write,
    fsync_dir,
    read_bytes_with_retry,
    replace_with_retry,
)
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

#: Data-home directory holding the document. A DIRECTORY leaf, not a file, so the
#: mask also covers the sibling temp ``atomic_write`` renames into place. The same
#: string is listed in ``sandbox._CREW_HIDDEN_LEAVES``,
#: ``sandbox._CREW_PRECREATE_HIDDEN_DIR_LEAVES`` and ``security._CREW_SECRET_LEAVES``.
TEAMS_DIR_NAME = "crew-teams"

#: The one document inside it.
TEAMS_FILE_NAME = "teams.json"

#: Document schema version, written on every save. Matches the convention of
#: the other gateway-owned documents so a later shape change keys on it instead
#: of inferring v1 from absence. A document carrying a NEWER version is
#: refused (``TeamsUnreadable``) rather than half-read.
TEAMS_SCHEMA_VERSION = 1

#: Serializes every read -> mutate -> rewrite of the document in this process.
#: The cross-process half is the advisory lock on :data:`LOCK_FILE_NAME`. Not
#: re-entrant: no caller holds it across another store call.
_WRITE_LOCK = threading.Lock()

#: Lock file beside the document; content is never meaningful, only the lock.
LOCK_FILE_NAME = ".lock"

#: Ceiling for acquiring the file lock; ``None`` is ``platform_compat``'s
#: default. A module constant so a test can shorten the wait while a holder
#: in another process keeps the lock.
LOCK_TIMEOUT_SECS: float | None = None

#: What a writer receives to learn the registered crew names. Evaluated INSIDE
#: the document lock, so the registry snapshot a write validates against is
#: the freshest one available. A removal in another process can still land
#: between that read and the write; the team then names a gone crewmate, which
#: every reader tolerates (``prune_unknown``) and which :func:`release_name`
#: clears the moment the name is registered again.
KnownCrews = Callable[[], set[str]]

#: Hard cap on a team name. Enforced on write and refused loudly, never
#: truncated: a name is what the roster header shows.
TEAM_NAME_MAX_CHARS = 80

#: Hard cap on how many teams one roster can hold. A bound on the document,
#: not a product limit anyone is expected to reach.
TEAMS_MAX = 200

#: Hard cap on the crewmates one team lists. Same posture as ``TEAMS_MAX``.
TEAM_MEMBERS_MAX = 500

#: A member is an exact crew name -- a free-form display name, bounded by the
#: same cap ``members.validate_member_name`` applies (``validation.MAX_SHORT_STRING``),
#: so every crew the roster lists can be teamed. Spelled as a literal because
#: ``validation`` imports ``security``, which imports this module through
#: ``memory_stores``; ``test_member_names_are_display_names_not_identifiers``
#: pins the two equal. Bounded on read as well as write.
MEMBER_NAME_MAX_CHARS = 500

#: Ids are minted by :func:`_new_team_id` -- 12 lowercase hex characters.
#: Always applied with ``fullmatch``: ``$`` alone admits a trailing newline.
_TEAM_ID_RE = re.compile(r"^[0-9a-f]{12}$")

#: Bytes a document may occupy. Enforced on WRITE (a save that would exceed it
#: is refused with ``teams_too_large`` and the document on disk is untouched)
#: and on READ (a larger file was not written by this module). Sized above the
#: largest document the caps allow -- see :func:`max_document_bytes` and the
#: test that pins the inequality -- so a valid sequence of writes can never
#: produce a file the read refuses.
TEAMS_FILE_MAX_BYTES = 320_000_000


def max_document_bytes() -> int:
    """Upper bound on a document every cap admits, in UTF-8 JSON bytes.

    Names are capped in CHARACTERS: a team name at ``TEAM_NAME_MAX_CHARS`` and a
    crew name at ``MEMBER_NAME_MAX_CHARS`` (the display-name cap), each up to 4
    bytes per character and up to 6 for a JSON escape. The arithmetic is deliberately
    loose (every entry at its widest, every character escaped) because the point
    is the inequality with :data:`TEAMS_FILE_MAX_BYTES`, not the exact size.
    """
    per_char = 6  # a JSON escape of a BMP character is 6 bytes; 4-byte UTF-8 is less
    member = MEMBER_NAME_MAX_CHARS * per_char + 4  # quotes, comma, space
    team = 64 + TEAM_NAME_MAX_CHARS * per_char + TEAM_MEMBERS_MAX * member  # keys + id + name
    return 64 + TEAMS_MAX * team


class TeamError(ValueError):
    """A team operation was refused; ``code`` is the machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TeamsUnreadable(RuntimeError):
    """``crew-teams/teams.json`` exists but cannot be read or parsed.

    Propagates rather than degrading to "no teams": a read that answered an
    empty list here would let the next write erase every team the user made.
    """


@dataclass
class Team:
    id: str
    name: str
    members: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "members": list(self.members)}


def teams_path() -> Path:
    """Absolute path to the teams document in its masked directory.

    Does NOT create the directory; :func:`write_teams` does on demand (the
    sandbox launcher pre-creates it empty before every spawn, so on a host
    that has spawned an agent it already exists).
    """
    return (data_home() / TEAMS_DIR_NAME / TEAMS_FILE_NAME).resolve()


@contextlib.contextmanager
def document_lock(directory: Path | None = None) -> Iterator[None]:
    """Hold both locks around one read -> mutate -> rewrite.

    *directory* names the store directory to lock; ``None`` is the live store.
    A restore names the data home it is restoring into, which is not always
    the one this process runs against.

    Public because the cross-process test holds it from a second interpreter.
    Not re-entrant: no in-process caller holds it across another store call.
    Lock order everywhere is the crew registry's lock FIRST (the dashboard's
    in-process config lock and the cross-process sidecar flock that
    ``update_config_locked`` holds), THEN this lock: the create hook
    (``release_for_create``) and the delete hook (``drop_member`` via
    ``after_write``) both run inside a registry hold and take this lock
    inside it, and the team routes take the in-process config lock before
    calling into the store. Nothing takes the registry's lock while holding
    this one -- ``KnownCrews`` reads the registry without locking -- so the
    order cannot invert. Raises ``OSError`` when the file lock cannot be
    taken within :data:`LOCK_TIMEOUT_SECS`.

    The directory is created here (owner-only, same as :func:`write_teams`)
    because the lock file has to exist before the document does: the first
    team the owner makes is itself a locked write.
    """
    store_dir = teams_path().parent if directory is None else directory
    with _WRITE_LOCK:
        store_dir.mkdir(parents=True, exist_ok=True)
        try:
            platform_compat.restrict_dir_to_owner(store_dir)
        except OSError:
            logger.debug("could not tighten mode on %s", store_dir, exc_info=True)
        with platform_compat.open_lock_file(store_dir / LOCK_FILE_NAME) as fd:
            with platform_compat.file_lock(
                fd, exclusive=True, wait=True, timeout=LOCK_TIMEOUT_SECS
            ):
                yield


def _new_team_id() -> str:
    # 12 hex chars: URL-safe, not guessable from the name, and short enough to
    # ride the page's ``?team=`` query parameter.
    return secrets.token_hex(6)


def _utf8_encodable(value: str) -> bool:
    """False for a string holding a lone surrogate (JSON-legal, UTF-8-illegal)."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def validate_team_name(name: object) -> str:
    """Return the stripped name, or raise :class:`TeamError`."""
    if not isinstance(name, str):
        raise TeamError("invalid_team_name", "team name must be a string")
    stripped = name.strip()
    if not stripped:
        raise TeamError("invalid_team_name", "team name must not be empty")
    if len(stripped) > TEAM_NAME_MAX_CHARS:
        raise TeamError("team_name_too_long", f"team name exceeds {TEAM_NAME_MAX_CHARS} characters")
    # JSON allows escaped lone surrogates; UTF-8 does not. Refuse them here
    # rather than letting the write raise mid-flight.
    if not _utf8_encodable(stripped):
        raise TeamError("invalid_team_name", "team name contains characters that cannot be encoded")
    # Refuse CONTROL characters (Cc: C0/C1, the escape sequences a terminal acts
    # on) and the two Unicode line/paragraph separators, which break a one-line
    # header. Judged by category, not ``isprintable()``: that also rejects format
    # characters (Cf) such as the zero-width joiner inside an emoji sequence, and
    # the no-break space (Zs), both legitimate in a name a user pasted.
    if any(unicodedata.category(ch) in ("Cc", "Zl", "Zp") for ch in stripped):
        raise TeamError("invalid_team_name", "team name contains control characters")
    return stripped


def _parse_teams(raw: bytes) -> list[Team]:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        # RecursionError: a document nested deeper than the interpreter's stack is
        # as unreadable as one that does not parse, and must surface as the same
        # coded 500 rather than an unstructured crash out of the handler.
        raise TeamsUnreadable(f"{TEAMS_FILE_NAME} is not valid JSON") from exc
    if not isinstance(doc, dict) or not isinstance(doc.get("teams"), list):
        raise TeamsUnreadable(f"{TEAMS_FILE_NAME} does not hold a team list")
    version = doc.get("version", TEAMS_SCHEMA_VERSION)
    if not isinstance(version, int) or version > TEAMS_SCHEMA_VERSION:
        raise TeamsUnreadable(
            f"{TEAMS_FILE_NAME} has schema version {version!r}, newer than this build reads"
        )
    if len(doc["teams"]) > TEAMS_MAX:
        raise TeamsUnreadable(f"{TEAMS_FILE_NAME} holds more than {TEAMS_MAX} teams")
    teams: list[Team] = []
    seen_ids: set[str] = set()
    seen_members: set[str] = set()
    for item in doc["teams"]:
        if not isinstance(item, dict):
            raise TeamsUnreadable(f"{TEAMS_FILE_NAME} holds a non-object team")
        team_id = item.get("id")
        name = item.get("name")
        members = item.get("members", [])
        # The same bounds the writers enforce, applied to what is RETAINED: a
        # restored or hand-edited document that violates a cap is refused
        # whole rather than served past the limit the caps declare.
        if (
            not isinstance(team_id, str)
            or not _TEAM_ID_RE.fullmatch(team_id)
            or team_id in seen_ids
        ):
            raise TeamsUnreadable(f"{TEAMS_FILE_NAME} holds a team with a bad id")
        if not isinstance(name, str) or not isinstance(members, list):
            raise TeamsUnreadable(f"{TEAMS_FILE_NAME} holds a malformed team")
        if not name.strip() or len(name) > TEAM_NAME_MAX_CHARS:
            raise TeamsUnreadable(f"{TEAMS_FILE_NAME} holds a team name outside the name bounds")
        # JSON admits an escaped lone surrogate that UTF-8 cannot encode. The
        # writers refuse it (validate_team_name), so a document carrying one
        # was not written here; retaining it would make the NEXT mutation's
        # ``encode("utf-8")`` raise mid-flight instead of this read refusing.
        if not _utf8_encodable(name):
            raise TeamsUnreadable(f"{TEAMS_FILE_NAME} holds a team name UTF-8 cannot encode")
        if len(members) > TEAM_MEMBERS_MAX:
            raise TeamsUnreadable(f"{TEAMS_FILE_NAME} holds a team with too many members")
        clean: list[str] = []
        for member in members:
            if not isinstance(member, str) or not member or len(member) > MEMBER_NAME_MAX_CHARS:
                raise TeamsUnreadable(
                    f"{TEAMS_FILE_NAME} holds a member name outside the name bounds"
                )
            if not _utf8_encodable(member):
                raise TeamsUnreadable(f"{TEAMS_FILE_NAME} holds a member name UTF-8 cannot encode")
            # The one-team invariant is enforced on every write; a document that
            # violates it was not written by this module. Reading it as-is would
            # render one crewmate twice, so the FIRST team keeps it -- the same
            # first-wins rule the write applies when it moves a crewmate.
            if member in seen_members or member in clean:
                continue
            clean.append(member)
        seen_ids.add(team_id)
        seen_members.update(clean)
        teams.append(Team(id=team_id, name=name, members=clean))
    return teams


def read_teams() -> list[Team]:
    """Every team, in the user's order. Absent file reads as no teams.

    Raises :class:`TeamsUnreadable` for a file that exists but cannot be read
    or parsed -- see the class docstring for why that is not an empty list.
    """
    path = teams_path()
    try:
        return read_document(path)
    except FileNotFoundError:
        return []
    except TeamsUnreadable as exc:
        _log_unreadable(path, str(exc))
        raise


def read_document(path: Path) -> list[Team]:
    """Read and fully validate the team document at *path*.

    The one reader every consumer shares: :func:`read_teams` for the live
    store, and the backup paths (`snapshot`, `portability`) for a document they
    are about to INSTALL -- a restore must refuse exactly what the live read
    would refuse, or it installs a file that fails every team route and every
    crew create with no in-product repair. ``FileNotFoundError`` propagates
    (the live read maps it to no teams); every other failure is
    :class:`TeamsUnreadable`.
    """
    try:
        # Bounded: the cap is enforced on what is READ, not on what was
        # allocated. A restored file far past the cap is refused after
        # ``TEAMS_FILE_MAX_BYTES + 1`` bytes instead of being loaded whole.
        raw = read_bytes_with_retry(path, max_bytes=TEAMS_FILE_MAX_BYTES + 1)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise TeamsUnreadable(f"could not read {path}") from exc
    if len(raw) > TEAMS_FILE_MAX_BYTES:
        raise TeamsUnreadable(f"{TEAMS_FILE_NAME} is larger than a team list can be")
    return _parse_teams(raw)


def _log_unreadable(path: Path, why: str) -> None:
    """Name the remedy where the operator will look for it.

    An unreadable document fails every team route AND refuses crew creation
    (:func:`release_name`), and no route can repair it -- a repair route would
    itself have to read the document it is repairing. The accepted remedy is
    the operator's shell: fix the file, or remove it, since an absent file
    reads as no teams. The API answers never carry the path (a dashboard
    reader is not necessarily the host's operator); the gateway log does.
    """
    logger.warning(
        "crew-teams document unreadable (%s): fix or remove %s -- an absent file reads as no teams",
        why,
        path,
    )


def write_teams(teams: list[Team]) -> None:
    """Persist the whole team list atomically (human write path only).

    fsync like the member bindings: a team the dashboard confirmed must not
    silently vanish to a crash. ``OSError`` propagates so the handler can tell
    the user the save did not land.
    """
    _write_document(teams_path(), teams)


def _write_document(path: Path, teams: list[Team]) -> None:
    """Atomically publish *teams* as the document at *path* (caller holds the lock)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Owner-only like every other gateway-owned leaf; best-effort tightening,
    # the sandbox mask and the file-tool fence are the real boundary.
    try:
        platform_compat.restrict_dir_to_owner(path.parent)
    except OSError:
        logger.debug("could not tighten mode on %s", path.parent, exc_info=True)
    payload = {"version": TEAMS_SCHEMA_VERSION, "teams": [t.to_dict() for t in teams]}
    encoded = json.dumps(payload, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > TEAMS_FILE_MAX_BYTES:
        # Refused before anything lands: a document the read would refuse must
        # never be written, or every later route answers ``teams_unreadable``.
        raise TeamError("teams_too_large", "the team list is too large to store")
    atomic_write(path, encoded, fsync=True)
    # The rename that publishes the file and, on first save, the directory's own
    # entry live in directory metadata a power-off can still lose. BEST-EFFORT:
    # the rename above has already committed the write, so a failing directory
    # sync must not be reported as a failed save -- the caller would answer 500
    # and a retry would create the team twice.
    fsync_dir(path.parent, best_effort=True)


def _has_document(dest: Path) -> bool:
    """True for a regular document at *dest*, False for no entry at all.

    Anything else -- a directory, a link, a device -- raises ``OSError``: the
    restore writers must not read such an entry as "absent" and then report a
    replace that left it in place as success while every team route still
    answers ``teams_unreadable``. Only a missing path is absent.
    """
    if platform_compat.is_link_or_junction(dest):
        raise OSError(f"{dest} is a link, not the team document")
    if not dest.exists():
        return False
    if not dest.is_file():
        raise OSError(f"{dest} is not a regular file")
    return True


def install_document(
    src: Path,
    directory: Path,
    *,
    only_if_absent: bool = False,
    save_existing: Callable[[Path], None] | None = None,
) -> bool:
    """Install the document *src* as the team list of the store at *directory*.

    The restore path's writer -- a snapshot or dashboard import putting a
    backed-up document in place -- and it writes the way every other writer
    does: under the store's lock, so a concurrent team write cannot commit its
    now-stale document over the restored one, and by replacing ONLY the
    document -- the directory and its lock file are never removed or replaced,
    because a writer already holding the lock file is what the lock protects
    against, and removing that file would hand it a lock nobody else can see.
    *src* is validated inside the lock with the same reader the live store
    uses (:func:`read_document`); ``TeamsUnreadable`` propagates. With
    *only_if_absent*, an existing document is left alone and False returned.
    An entry at the document's name that is not a regular file (a directory, a
    link) is never read as "absent": ``OSError`` propagates and the restore
    reports the target it could not write instead of preserving it silently.
    *save_existing* is called INSIDE the lock with the live document, when there
    is one, before it is replaced: a restore's rollback copy taken outside the
    lock could miss a write that commits between the copy and the install, and
    that write would then be lost from both the live store and the rollback.
    """
    dest = directory / TEAMS_FILE_NAME
    with document_lock(directory):
        present = _has_document(dest)
        if only_if_absent and present:
            return False
        if save_existing is not None and present:
            save_existing(dest)
        _write_document(dest, read_document(src))
    return True


def remove_document(
    directory: Path, *, save_existing: Callable[[Path], None] | None = None
) -> bool:
    """Remove the store's document under its lock (a restore undoing an install).

    *save_existing* as in :func:`install_document`: the rollback copy is taken
    inside the same lock hold that removes the document. Only a MISSING path
    returns False; a directory or link at the name raises ``OSError`` so a
    document-less replace cannot succeed while leaving it there.
    """
    dest = directory / TEAMS_FILE_NAME
    with document_lock(directory):
        if not _has_document(dest):
            return False
        if save_existing is not None:
            save_existing(dest)
        dest.unlink()
        # The unlink alone is not durable: a power loss before the directory's
        # metadata reaches disk brings the removed document back on the next
        # boot, silently undoing a removal the restore reported as done. Same
        # sync the two sibling writers run (``_write_document``,
        # ``install_document``), inside the same lock hold.
        fsync_dir(directory, best_effort=True)
    return True


def restore_document(saved: Path, directory: Path) -> None:
    """Put a rollback copy back VERBATIM, under the store's lock.

    The undo half of :func:`install_document`. The saved copy is whatever the live
    store held before the restore -- possibly a document the reader already
    refused -- and recovery's job is to put that state back, not to judge it, so
    the bytes are copied as they are and nothing here can refuse them.
    ``OSError`` propagates; the caller names the target it could not undo.
    The copy is STREAMED into a temporary file beside the document and published
    by rename: the saved copy is whatever size the live store held, which the
    reader's cap does not bound, and a rollback that read it whole could exhaust
    memory and strand every later target of the same recovery.
    """
    dest = directory / TEAMS_FILE_NAME
    with document_lock(directory):
        fd, tmp_name = tempfile.mkstemp(prefix=f".{TEAMS_FILE_NAME}.", dir=str(directory))
        tmp = Path(tmp_name)
        try:
            with saved.open("rb") as src, os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            replace_with_retry(tmp, dest)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
        fsync_dir(directory, best_effort=True)


def prune_unknown(teams: list[Team], known: set[str]) -> list[Team]:
    """*teams* with every member not in *known* dropped -- a read-side VIEW.

    Every crew-removal path calls :func:`drop_member` best-effort (the
    dashboard delete inside the registry lock, the package-sync prune for the
    names it actually deleted, ``kirocrew agent delete`` from its own process).
    This is the second line: a drop that failed, or a document edited by a
    build that predates one of those hooks, must still never be ANSWERED as
    membership. The document is left as written; the next membership write
    re-validates it anyway. The third line is :func:`release_name` on every
    create path: a name this view hides only because the registry lacks it is
    purged from the document before the registry gains it again.
    """
    return [Team(id=t.id, name=t.name, members=[m for m in t.members if m in known]) for t in teams]


def _validate_names(members: object) -> list[str]:
    """Shape and bounds only -- the list a write may RETAIN, registry aside."""
    if not isinstance(members, list):
        raise TeamError("invalid_members", "members must be a list of crewmate names")
    if len(members) > TEAM_MEMBERS_MAX:
        raise TeamError("too_many_members", f"a team lists at most {TEAM_MEMBERS_MAX} crewmates")
    out: list[str] = []
    for member in members:
        if not isinstance(member, str) or not member:
            raise TeamError("invalid_members", "members must be a list of crewmate names")
        # The read side refuses a member past MEMBER_NAME_MAX_CHARS or one UTF-8
        # cannot encode (_parse_teams), so the write side must refuse it FIRST,
        # before the registry lookup: a registry key that slipped past the
        # roster's grammar must never be retained into a document the reader
        # would then refuse whole.
        if len(member) > MEMBER_NAME_MAX_CHARS or not _utf8_encodable(member):
            raise TeamError("invalid_members", "members must be a list of crewmate names")
        if member not in out:
            out.append(member)
    return out


def _validate_members(members: object, known: set[str]) -> list[str]:
    out = _validate_names(members)
    for member in out:
        if member not in known:
            raise TeamError("unknown_member", f"no crewmate named {member!r}")
    return out


def _detach(teams: list[Team], members: list[str], *, keep: str | None) -> None:
    """Remove *members* from every team except *keep* (the one-team invariant)."""
    for team in teams:
        if team.id == keep:
            continue
        team.members = [m for m in team.members if m not in members]


def _find(teams: list[Team], team_id: str) -> Team:
    for team in teams:
        if team.id == team_id:
            return team
    raise TeamError("team_not_found", f"no team with id {team_id!r}")


def create_team(name: object, members: object, *, known: KnownCrews) -> Team:
    """Append a team; moves the listed crewmates out of their current team.

    *known* returns the registered crew names every listed member must be in;
    it is called inside the document lock (see :data:`KnownCrews`).
    """
    clean_name = validate_team_name(name)
    with document_lock():
        clean_members = _validate_members(members, known())
        teams = read_teams()
        if len(teams) >= TEAMS_MAX:
            raise TeamError("too_many_teams", f"at most {TEAMS_MAX} teams")
        team_id = _new_team_id()
        while any(t.id == team_id for t in teams):  # pragma: no cover - 48 random bits
            team_id = _new_team_id()
        _detach(teams, clean_members, keep=None)
        team = Team(id=team_id, name=clean_name, members=clean_members)
        teams.append(team)
        write_teams(teams)
    return team


def update_team(
    team_id: str,
    *,
    name: object | None = None,
    members: object | None = None,
    add: object | None = None,
    remove: object | None = None,
    known: KnownCrews,
) -> Team:
    """Rename and/or re-member one team. Omitted fields are left unchanged.

    Membership changes come in two shapes. ``members`` REPLACES the list.
    ``add`` / ``remove`` are DELTAS applied to the list as it stands inside the
    lock, so a caller holding a stale copy (a dialog opened before another tab
    moved a crewmate) changes only the crewmates it touched and never writes
    its snapshot back over the newer one. The two shapes are exclusive.
    ``remove`` need not name registered crews: a deleted crew's stale entry is
    exactly what a caller may want gone.
    """
    if name is None and members is None and add is None and remove is None:
        raise TeamError("nothing_to_update", "name or members required")
    if members is not None and (add is not None or remove is not None):
        raise TeamError("invalid_members", "members cannot be combined with add or remove")
    clean_name = validate_team_name(name) if name is not None else None
    with document_lock():
        registry = known() if members is not None or add is not None else set()
        clean_members = _validate_members(members, registry) if members is not None else None
        clean_add = _validate_members(add, registry) if add is not None else []
        clean_remove = _validate_names(remove) if remove is not None else []
        teams = read_teams()
        team = _find(teams, team_id)
        if clean_name is not None:
            team.name = clean_name
        if clean_members is not None:
            _detach(teams, clean_members, keep=team.id)
            team.members = clean_members
        if add is not None or remove is not None:
            kept = [m for m in team.members if m not in clean_remove]
            joining = [m for m in clean_add if m not in kept]
            if len(kept) + len(joining) > TEAM_MEMBERS_MAX:
                raise TeamError(
                    "too_many_members", f"a team lists at most {TEAM_MEMBERS_MAX} crewmates"
                )
            _detach(teams, joining, keep=team.id)
            team.members = kept + joining
        write_teams(teams)
    return team


def delete_team(team_id: str) -> None:
    """Remove a team; its crewmates simply have no team afterwards."""
    with document_lock():
        teams = read_teams()
        team = _find(teams, team_id)
        teams.remove(team)
        write_teams(teams)


def drop_member(name: str) -> bool:
    """Remove a crewmate from whichever team lists it (a deleted crew).

    Returns True when a write happened. Best-effort by contract, on EVERY
    removal path (dashboard delete, ``kirocrew agent delete``, package prune):
    the crew's config write has already committed, and a team list that still
    names a gone crewmate is tolerated by every reader (``prune_unknown`` hides
    it; the roster has no row for it). So an unreadable document AND a failed
    rewrite (disk full, a permission change on ``crew-teams/``) are both logged
    and swallowed here -- neither may turn a completed crew delete into a 500.
    The one harm a stale entry could do -- a crew later RECREATED under the
    same name silently rejoining the old team -- is closed where it would
    materialize, by :func:`release_name` on every create path.

    Called AFTER the removal's registry write has committed and still INSIDE
    its lock (``update_config_locked(after_write=...)``): after the commit, so
    a registry write that fails leaves the membership as it was; inside the
    lock, so a same-name create in another process (which needs that same
    lock) cannot land between the delete and the drop and have its fresh
    membership dropped instead.
    """
    try:
        with document_lock():
            teams = read_teams()
            changed = False
            for team in teams:
                if name in team.members:
                    team.members = [m for m in team.members if m != name]
                    changed = True
            if changed:
                write_teams(teams)
    except (TeamsUnreadable, OSError):
        logger.warning("could not drop %r from its team", name, exc_info=True)
        return False
    return changed


class TeamsUnavailable(Exception):
    """:func:`release_for_create` could not purge a name: the document exists
    but cannot be read or rewritten. The create that asked is refused."""


def release_for_create(name: str) -> None:
    """:func:`release_name` for a create path's locked mutation.

    Called by ``persist_member_config`` on EVERY create (no create path can
    opt out), INSIDE the registry's cross-process locked mutation, after that
    mutation has established the name is free and before it registers it, so
    the purge and the registration are one critical section: no other process
    can create and team the same name in between (which a purge outside the
    lock would then erase). The package sync, which registers through its own
    locked mutation, calls it from there. Folds the store's failures into one
    typed exception the create paths answer as "teams unavailable".
    """
    try:
        release_name(name)
    except (TeamsUnreadable, OSError) as exc:
        raise TeamsUnavailable(str(exc)) from exc


def release_name(name: str) -> bool:
    """Drop ``name`` from any team BEFORE a crew is registered under it.

    The create-time half of the no-inheritance contract: whatever removal path
    failed to drop the previous holder of this name (every one of them is
    best-effort), the new crew starts on no team. Returns True when a write
    happened. NOT best-effort -- ``TeamsUnreadable`` and ``OSError`` propagate,
    and the caller refuses the create: registering the name while its stale
    membership cannot be purged is exactly what would expose it. An ABSENT
    document is the common case (no teams were ever made) and answers False --
    but only once the lock is held: absence decided BEFORE the lock races a
    restore or import installing a document in the gap, and the name would
    then inherit whatever that document lists. The store directory is
    materialised by the lock (the sandbox pre-creates it before every spawn
    anyway); no document is written for an absent store.

    Every caller runs this (via :func:`release_for_create`) inside the crew
    registry's locked mutation, and every removal path runs
    :func:`drop_member` inside its own; lock order is the registry's sidecar
    lock first, then the document lock.
    """
    # Absence is decided by stat, not ``Path.exists``: ``exists`` folds every
    # ``OSError`` into False, and a directory this process cannot stat into
    # (left owned by another user at 0700) would then read as "no teams" and
    # let the name inherit whatever the document lists. Only a missing entry
    # is "no document"; any other failure propagates and refuses the create.
    # The stat runs INSIDE the lock: every writer that can put a document here
    # (``install_document`` from a restore or import included) holds this
    # lock, so an absence read under it cannot be overtaken before the
    # caller registers the name.
    with document_lock():
        try:
            teams_path().stat()
        except FileNotFoundError:
            return False
        teams = read_teams()
        changed = False
        for team in teams:
            if name in team.members:
                team.members = [m for m in team.members if m != name]
                changed = True
        if changed:
            write_teams(teams)
    return changed
