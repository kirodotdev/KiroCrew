"""Authenticated record of which nudge loops a crew/member session armed ITSELF.

``NudgeLoop.self_armed`` is the one bit that relaxes the crew/member
external-arm refusal at fire time (``GatewayOrchestrator._fire_dashboard_nudge``),
and the loop store it lives in (``autonudge.json``) is agent-writable: an
agent -- or a prompt injected into one -- can write ``"self_armed": true`` on a
loop it did not arm from that session, and a restart would restore it. The
persisted bit alone is therefore not authorization; it is a hint that must
AGREE with a record the agent cannot forge.

That record lives here, in a GATEWAY-ONLY directory of its own,
``tag-grants/autonudge-trust/`` (:func:`self_arm_record_path`): a child of the
chat_tag grant store's root, chosen because that root is ALREADY a
whole-directory stand-in mask -- bind-masked by ``sandbox._CREW_HIDDEN_LEAVES``
and pre-created 0o700 before every spawn -- that holds every name inside it for
the namespace lifetime, present and future. A leaf masked at the data-home root
holds only the object present at spawn: a host-side atomic replace of that name
puts a fresh, writable object at the protected path for the rest of a running
namespace's life, which for an authorization record is the hole itself. Inside
the grant store's mask the record has no such window, its name reaches the
launcher payload as its own nested leaf (``tag-grants/autonudge-trust``, so the
hold is measured by ``test_sandbox_protected_name_holds``), and all three ways
of writing it are closed: the agent's file tools are fenced by
``security._CREW_SECRET_LEAVES`` (``tag-grants`` is prefix-matched, so the whole
subtree is fenced), a sandboxed shell -- a command that builds the path at
runtime, which no text matcher sees -- resolves inside the empty stand-in, and
no in-sandbox code opens it: only gateway code opening the path directly -- the
authorizer at the moment it admits an arm, the owner's switch, the store's
remove -- ever reads or writes it. The grant store's root is the right host
because it already holds owner authorization state of the same class (which
tags an agent may self-apply), is written by the gateway alone, and is never
swept or staged through; its own files stay siblings of this directory, never
inside it. NOT under ``trust/``: that directory is a declared sandbox READ-WRITE
exception (in-sandbox MCP servers append to the audit log there and
``verify_session_pid`` reads the SEL key), so a record under it stayed writable
by a same-UID sandboxed command, and an entry is the whole of an owner arm's
fire-time admission. A record an upgraded install still has at the ``trust/``
layout is DISCARDED on first access, not migrated
(:func:`_retire_legacy_record`): its entries may be the sandbox's, so the loops
behind them are refused until armed again. The fire-time guard admits a crew/member wake
only when BOTH hold: ``loop.self_armed is True`` on the record it loaded AND
``is_recorded_self_arm(loop.id, loop.slot_key)`` here. A forged bit with no
trust entry refuses; a stale trust entry with no bit refuses.

One flat JSON object ``{loop_id: {"slot_key": ..., "armed_ts": ...}}`` under a
SEAL: an HMAC over the entries keyed by the gateway's ``token_signing.key``
(:func:`_seal`), a secret every shipping build already masks from the sandbox
and fences from the agent's file tools. The directory mask above closes the
record to the sandbox from the boot that first masks it; it says nothing about
bytes planted at the same path BEFORE that boot, when the name was an ordinary
writable child of the data home. The seal does: a record this gateway did not
write under its current key cannot carry a valid seal, so a pre-upgrade plant
reads as nothing recorded (refuse) and is moved aside by the next writer
(:func:`_quarantine_unsealed_record`). A token-key rotation breaks every seal
the same way -- the safe side; the loops behind them are armed again. An entry
is written by exactly one path (the authorizer admitting a self-arm) and REVOKED
by exactly one path: its loop leaving the store (``AutoNudgeService.remove_sync``
calls :func:`forget_self_arm` on remove and on replacement), so a removed loop's
id cannot keep its authorization and the file cannot grow past the loops that
were armed and not yet removed. A write never prunes against a caller-supplied
view of the store -- see :func:`record_self_arm` for the race that would open.
Every read-modify-write runs under an exclusive file lock so two concurrent
self-arms cannot drop each other's entries, with the record's host and its own
directory HELD open for the whole transaction (:func:`_hold_record_dir`): a link
at either name is refused by the open itself, never screened and then followed,
and the read, the quarantine and the publish inside address the record relative
to the held directory where the platform can. Every reader is TOTAL: a
missing, unreadable, malformed or unsealed file reads as "not recorded", which is the
refusing answer.

Blocking file IO throughout -- async callers offload via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterator

from kiro_crew import atomic_write as atomic_write_module
from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

SELF_ARM_RECORD_NAME = "autonudge-self-armed.json"
_LOCK_NAME = SELF_ARM_RECORD_NAME + ".lock"

#: The EXISTING gateway-only directory the record's own directory sits inside:
#: the chat_tag grant store's root (``chat_tag_grants._STORE_SUBDIR``, pinned
#: equal by test). Bind-masked WHOLE as a stand-in before every spawn
#: (``sandbox._CREW_HIDDEN_LEAVES`` + ``_CREW_PRECREATE_HIDDEN_DIR_LEAVES``),
#: fenced from the agent file tools by prefix (``security._CREW_SECRET_LEAVES``),
#: created at boot by the grant store's own seed and never swept: the durable
#: hold the record needs, already in force. Spelled here rather than imported,
#: because the grant store module loads ``token_secret`` on import and this one
#: must not (see :func:`_seal_secret`).
ARM_RECORD_HOST_DIRNAME = "tag-grants"

#: The record's own directory, a child of that host, named for what it holds
#: and for nothing else, so no other writer has a reason to share it. Created
#: lazily by the gateway's first write (:func:`_hold_record_dir`, which then
#: holds it open for the transaction) and NOT pre-created for the sandbox: it is
#: inside the host's mask, so a sandbox spawned before the first arm sees the
#: host's empty stand-in and never this name, whether or not it exists yet.
ARM_RECORD_DIRNAME = "autonudge-trust"

#: The data-home-relative leaf the sandbox names for the record's directory.
#: Listed in ``sandbox._CREW_HIDDEN_LEAVES`` beneath its host so the launcher
#: payload carries the path and ``test_sandbox_protected_name_holds`` can prove
#: the hold is the HOST's stand-in (``HELD_BY_ENCLOSING_MASK``), not a leaf-only
#: mask at the data-home root. Never pre-created and never in
#: ``_CREW_NO_ALIAS_LEAVES``: both jobs are the host's.
ARM_RECORD_LEAF = f"{ARM_RECORD_HOST_DIRNAME}/{ARM_RECORD_DIRNAME}"

#: The directory an older layout kept the record in. Read only by
#: :func:`_retire_legacy_record`, which deletes what it finds there.
_LEGACY_DIRNAME = "trust"

#: ``armed_by`` values an entry may carry. Absent reads as ``ARMED_BY_SELF``.
ARMED_BY_SELF = "self"
ARMED_BY_OWNER = "owner"

#: Domain separator for the record's seal, so a value minted here can never
#: validate as a dashboard token, a tag-grant certificate, or any other HMAC
#: the same key signs -- and vice versa.
_SEAL_DOMAIN = b"kiro-crew:autonudge-trust:seal:v1\x00"

#: Where :func:`_quarantine_unsealed_record` moves a parseable record that does
#: not carry this gateway's seal: beside the record, same masked directory, as
#: evidence rather than deleted. One name, so a second quarantine replaces the
#: first and the directory cannot grow with plants.
_UNSEALED_SUFFIX = ".unsealed"


def _seal_secret() -> bytes:
    """The key the seal is minted under: the dashboard's ``token_signing.key``.

    Chosen because it is the one gateway secret that every build shipping this
    record already keeps out of both agent planes (``sandbox._CREW_HIDDEN_LEAVES``
    masks it from spawned commands, ``security._CREW_SECRET_LEAVES`` fences it
    from the file tools), so a plant written before THIS leaf was masked could
    not have read it. Lazy import: ``token_secret`` must not be loaded -- and
    must not create its key file -- on import of this module.
    """
    from kiro_crew.dashboard import token_secret

    return token_secret._get_secret()


def _seal(entries: dict[str, Any]) -> str:
    """HMAC-SHA256 over the canonical JSON of *entries* under :func:`_seal_secret`."""
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hmac.new(
        _seal_secret(), _SEAL_DOMAIN + canonical.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _is_sealed(raw: dict[str, Any], entries: dict[str, Any]) -> bool:
    """Whether *raw* carries a seal that verifies over *entries*.

    Total: a missing or non-string seal, or a key that cannot be loaded, is
    ``False`` -- the refusing answer -- never an exception into a reader.
    """
    seal = raw.get("seal")
    if not isinstance(seal, str):
        return False
    try:
        return hmac.compare_digest(seal, _seal(entries))
    except Exception:
        logger.warning("autonudge trust record seal could not be checked", exc_info=True)
        return False


@contextlib.contextmanager
def _record_lock() -> Iterator[int]:
    """Exclusive lock spanning one read-modify-write transaction, chain HELD throughout.

    Two self-arms committing at once (two members arming in the same second)
    would otherwise each read the pre-transaction file and the second write
    would drop the first's entry -- a loop the authorizer reported as armed
    that the fire-time guard then refuses. A sibling lock file rather than the
    record itself, because ``atomic_write`` replaces the record's inode.

    The record's host and its own directory are opened by :func:`_hold_record_dir`
    BEFORE the lock and stay open until the transaction ends, and the body is
    handed the descriptor of the record's directory: every read, the quarantine
    rename and the publishing write inside address the record RELATIVE to it
    where the platform can (:func:`_descriptor_relative`), so no name below the
    data home is resolved again once it has been proved. Where it cannot -- the
    platform without ``openat``, which is also the one with junctions -- the held
    handles withhold delete sharing, so neither directory can be renamed or
    removed while the by-name uses below run, and the object the open proved is
    the object they reach. The lock file itself is opened the same way, so a link
    planted at its name is refused rather than locked through.
    """
    path = self_arm_record_path()
    held = _hold_record_dir(path.parent)
    try:
        record_dir_fd = held[-1]
        lock_fd = _open_lock_no_follow(path.parent, record_dir_fd)
        try:
            with platform_compat.file_lock(lock_fd, exclusive=True):
                _retire_legacy_record()
                _quarantine_unsealed_record(record_dir_fd)
                yield record_dir_fd
        finally:
            os.close(lock_fd)
    finally:
        _release_held(held)


def self_arm_record_path() -> Path:
    """Absolute path of the record: ``<data home>/tag-grants/autonudge-trust/<name>``."""
    return data_home() / ARM_RECORD_HOST_DIRNAME / ARM_RECORD_DIRNAME / SELF_ARM_RECORD_NAME


def _legacy_record_path() -> Path:
    return data_home() / _LEGACY_DIRNAME / SELF_ARM_RECORD_NAME


def _descriptor_relative() -> bool:
    """Whether this platform can create, open, read and publish RELATIVE to a directory fd.

    :func:`atomic_write.pinned_parent_replace_supported` is the write half (an
    ``O_NOFOLLOW`` staged temp and a ``renameat`` publish); the directory walk
    below also needs ``mkdirat`` and ``O_DIRECTORY``. One predicate for all of
    them, so a platform that had some but not all could never be half-pinned.
    """
    return (
        atomic_write_module.pinned_parent_replace_supported()
        and hasattr(os, "O_DIRECTORY")
        and os.mkdir in os.supports_dir_fd
    )


def _dir_open_flags() -> int:
    """Open flags for a held directory: read-only, a directory, never through a link."""
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _hold_record_dir(directory: Path) -> list[int]:
    """Create the record's host and its own directory if absent, and return them HELD open.

    The two components are the ones below the data home -- ``tag-grants`` and its
    ``autonudge-trust`` child -- outermost first; the caller releases them with
    :func:`_release_held` once the transaction they protect is over. A link at
    either name is REFUSED, never removed and never followed, and the refusal is
    the open itself rather than a verdict taken first and trusted after: each
    component is created with a plain ``mkdir`` (which never traverses a link at
    the name it creates) and then opened by :func:`platform_compat.pin_directory`
    -- ``O_DIRECTORY | O_NOFOLLOW`` on POSIX, a reparse point opened AS ITSELF and
    then rejected on Windows -- so a symlink, junction or plain file at the name
    fails the open and the arm fails closed (``OSError`` to the authorizer, which
    then reports the loop as not armed). Nothing here asks "is this a link?" and
    then proceeds by name, which is the window a link planted between the two
    would slip through.

    Where the platform can address a name relative to a descriptor
    (:func:`_descriptor_relative`), the child is created and opened RELATIVE to the
    held host, so a host swapped for a link after its own open cannot steer the
    child's creation. Where it cannot, the host's handle withholds delete sharing
    for as long as it is held, so the host can be neither renamed nor removed
    while the child is created and opened by name. The residual there is the one
    :func:`platform_compat.pin_directory` documents: reparse data set IN PLACE on
    a held directory, which Windows accepts only while that directory is EMPTY --
    a fresh host with no grant store beside the child yet.

    Modes are hygiene, not the boundary (the fence and the mask are): applied
    through the held descriptor on POSIX so the umask-masked ``mkdir`` mode is
    corrected on the object that was opened, by name on Windows where the DACL
    helper has no descriptor form and the held handle keeps the name bound.
    A failure to tighten is logged, never raised.

    A failure part-way releases what was taken: a half-held chain protects nothing.
    """
    host, child = directory.parent, directory
    relative = _descriptor_relative()
    held: list[int] = []
    try:
        with contextlib.suppress(FileExistsError):
            os.mkdir(host, 0o700)
        held.append(_pin_held(host))
        if relative:
            with contextlib.suppress(FileExistsError):
                os.mkdir(child.name, 0o700, dir_fd=held[-1])
            try:
                held.append(os.open(child.name, _dir_open_flags(), dir_fd=held[-1]))
            except OSError as exc:
                raise OSError(
                    f"{child} could not be held open; the autonudge trust record is not "
                    "written under it"
                ) from exc
        else:
            with contextlib.suppress(FileExistsError):
                os.mkdir(child, 0o700)
            held.append(_pin_held(child))
        for component, fd in zip((host, child), held):
            try:
                if relative:
                    # 0o700 is owner-only and keeps directory traversal; Semgrep's
                    # 0o644 suggestion adds world-read and removes traversal.
                    # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501
                    os.chmod(fd, 0o700)
                else:
                    platform_compat.restrict_dir_to_owner(component)
            except OSError:
                logger.debug("could not tighten mode on %s", component, exc_info=True)
    except BaseException:
        _release_held(held)
        raise
    return held


def _pin_held(component: Path) -> int:
    """Hold *component* open as a real directory, refusing a link at its name.

    Wraps every failure but absence into one ``OSError`` naming the component --
    POSIX reports a symlink as ``ELOOP`` and a file as ``ENOTDIR``, Windows opens
    the reparse point and raises off the descriptor -- so the authorizer's log line
    carries the whole diagnosis. Absence is left as ``FileNotFoundError``: the
    ``mkdir`` before it should have made the name, so an absent component means the
    data home itself is gone.
    """
    try:
        return platform_compat.pin_directory(component)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise OSError(
            f"{component} could not be held open (a link, or not a directory); the "
            "autonudge trust record is not written under it"
        ) from exc


def _release_held(held: list[int]) -> None:
    """Close a held chain, innermost first, and keep going when one will not close."""
    for fd in reversed(held):
        try:
            os.close(fd)
        except OSError:
            logger.debug("a held autonudge trust record directory would not close")


#: How many create-or-open rounds :func:`_open_lock_no_follow` makes before it
#: gives up. Two is the race it exists for (lose the create, find the winner's
#: file); the rest cover a lock file something outside this module removes
#: between those two steps, which nothing in the tree does.
_LOCK_OPEN_ATTEMPTS = 4


def _open_lock_no_follow(directory: Path, dir_fd: int) -> int:
    """Open (creating if absent) the sibling lock file, refusing a link at its name.

    Created EXCLUSIVELY first, and the loser of that race then opens the winner's
    file without ``O_CREAT``. Never a single nonexclusive ``O_CREAT`` open: Darwin
    can answer ENOENT to concurrent ``openat(O_CREAT)`` calls on an absent name --
    the same interleaving ``platform_log_append`` and ``apps.backend`` elect one
    creator for -- and this is exactly the shape two members arming in the same
    second produce, since every transaction opens this one lock file. Both opens
    carry ``O_NOFOLLOW`` and both are made relative to the held record directory
    where the platform can, so a link planted at the lock's name is refused
    (``O_EXCL`` sees it as existing, the plain open refuses to follow it) rather
    than locked through, and neither resolves the directory by name again.
    ``O_RDWR``, not read-only: the Windows lock primitive needs write access.
    """
    base = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    relative = _descriptor_relative()
    name: str | Path = _LOCK_NAME if relative else directory / _LOCK_NAME
    at: dict[str, int] = {"dir_fd": dir_fd} if relative else {}
    for _ in range(_LOCK_OPEN_ATTEMPTS):
        try:
            return os.open(name, base | os.O_CREAT | os.O_EXCL, 0o600, **at)
        except FileExistsError:
            pass
        try:
            return os.open(name, base, **at)
        except FileNotFoundError:
            # The winner's file is gone again before this open reached it.
            # Nothing here removes it, so this is an outside actor; try the
            # election once more rather than creating nonexclusively.
            continue
    raise OSError(
        f"{directory / _LOCK_NAME} could not be created or opened after "
        f"{_LOCK_OPEN_ATTEMPTS} attempts; the autonudge trust record is not written"
    )


def _open_record_no_follow(path: Path, dir_fd: int | None) -> int:
    """Open the record for reading, relative to the held directory where the platform can.

    Raises what ``os.open`` raises; ``FileNotFoundError`` is the callers' "no record".
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if dir_fd is not None and _descriptor_relative():
        return os.open(path.name, flags, dir_fd=dir_fd)
    return os.open(path, flags)


def _retire_legacy_record() -> None:
    """Delete a record left at the ``trust/`` layout. It is NOT migrated.

    That file sat in a directory the sandbox keeps read-write, so anything in
    it may have been written by a sandboxed command rather than by the
    authorizer -- carrying it into the masked leaf would launder a forged entry
    into a trusted one, and there is no way to tell the two apart after the
    fact. So the whole file is discarded, and every loop armed before the
    upgrade is refused at fire time (the safe side, audited by the fire guard)
    until it is armed again: the owner's switch goes OFF then ON, a crewmate's
    own loop is re-armed from its turn. One-time and gateway-only, called under
    the record lock by every writer and (lockless) by a reader that finds the
    new path absent; idempotent once the file is gone. Best-effort: an
    ``OSError`` is logged and the caller proceeds on the new path.
    """
    legacy = _legacy_record_path()
    try:
        if not legacy.is_file():
            return
        legacy.unlink()
        logger.warning(
            "legacy autonudge trust record at %s discarded (that directory is writable "
            "from the agent sandbox, so its entries cannot be trusted); loops armed before "
            "this upgrade must be armed again",
            legacy,
        )
        with contextlib.suppress(OSError):
            (legacy.parent / _LOCK_NAME).unlink()
    except OSError:
        logger.warning("could not discard the legacy autonudge trust record", exc_info=True)


def _load_record_file(
    path: Path, dir_fd: int | None = None
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """``(raw, entries)`` of a parseable record of the expected shape, else ``None``.

    Raises what the open/``json.loads`` raise; the callers decide what an
    unreadable or mis-shaped file means to them (the total readers say ``{}``,
    the strict one raises). A ``None`` is the mis-shaped case only. Inside a
    :func:`_record_lock` transaction *dir_fd* is the held record directory and
    the file is opened relative to it, so the bytes a writer reads back come from
    the directory its publish will land in.
    """
    fd = _open_record_no_follow(path, dir_fd)
    try:
        fh = os.fdopen(fd, "r", encoding="utf-8")
    except Exception:
        # ``fdopen`` takes ownership of the descriptor only on success.
        os.close(fd)
        raise
    with fh:
        raw = json.loads(fh.read())
    entries = raw.get("loops") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return None
    return raw, entries


def _quarantine_unsealed_record(dir_fd: int) -> None:
    """Move a parseable record that does not carry this gateway's seal aside.

    Called under :func:`_record_lock` by every writer, so no reader can be
    quarantining a file a writer just sealed: the writer holding the lock is
    the only mover. A file that does not PARSE is left where it is -- the
    strict reader refuses the write on it and keeps it as evidence, since a
    torn write of the gateway's own is a possibility there; a file that parses
    to the right shape but carries no valid seal has exactly one story (not
    written by this gateway under its current key -- a plant from before the
    leaf was masked, or a record from before a token-key rotation) and no
    reading under which its entries may be kept. Best-effort: an ``OSError``
    is logged and the strict reader still refuses its content. *dir_fd* is the
    held record directory from :func:`_record_lock`: the read and the rename both
    go through it where the platform can, so the file judged is the file moved.
    """
    path = self_arm_record_path()
    try:
        loaded = _load_record_file(path, dir_fd)
    except (OSError, ValueError):
        return
    if loaded is None or _is_sealed(*loaded):
        return
    try:
        aside = path.with_name(path.name + _UNSEALED_SUFFIX)
        if _descriptor_relative():
            # ``os.rename`` replaces an existing destination on POSIX, which is
            # the platform this branch runs on; ``os.replace`` has no dir_fd form.
            os.rename(path.name, aside.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        else:
            os.replace(path, aside)
        logger.warning(
            "autonudge trust record at %s carries no valid seal (not written by this gateway "
            "under its current key) and was moved aside; loops armed under it must be armed again",
            path,
        )
    except OSError:
        logger.warning("could not quarantine the unsealed autonudge trust record", exc_info=True)


def _read_record() -> dict[str, dict[str, Any]]:
    """Return the record's entries, or ``{}`` for absent/unreadable/malformed/unsealed."""
    path = self_arm_record_path()
    if not path.exists():
        # A reader on an upgraded install before any writer ran. Not under the
        # record lock: a reader may run inside a writer's locked section
        # (``restore_arm_party_if_token`` reads before it writes) and the lock
        # is not re-entrant; the unlink is idempotent, so a racing writer's own
        # retirement simply wins and this one logs and falls through to a read.
        _retire_legacy_record()
    try:
        loaded = _load_record_file(path)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        logger.warning("autonudge self-arm record unreadable; treating as empty", exc_info=True)
        return {}
    if loaded is None:
        return {}
    raw, entries = loaded
    if not _is_sealed(raw, entries):
        logger.warning("autonudge trust record carries no valid seal; treating as empty")
        return {}
    return {
        str(loop_id): entry
        for loop_id, entry in entries.items()
        if isinstance(entry, dict) and isinstance(entry.get("slot_key"), str)
    }


def _read_record_strict_raw(dir_fd: int | None = None) -> dict[str, Any]:
    """The record's ``loops`` map VERBATIM, for the writers.

    Unlike :func:`_read_record`, which is the readers' total view, this one
    keeps every entry as stored -- a malformed sibling included -- and RAISES
    ``OSError`` when the file exists but cannot be read or is not the expected
    shape. A writer that read a corrupt file as ``{}`` would then write its one
    entry over every sibling's, turning one bad byte into every other member's
    loop being refused at its next wake; refusing the write keeps the file as
    evidence and the caller fails closed. A missing file is an empty map: nothing
    to lose.

    A file that parses but carries no valid seal is NOT indeterminate: it was
    provably not written by this gateway under its current key, so its entries
    are nobody's and it reads as the empty map -- the certain "nothing
    recorded" answer, which lets a writer proceed (the lock quarantines the file
    first) rather than leaving the switch refused until an operator intervenes.
    """
    path = self_arm_record_path()
    if not path.exists():
        _retire_legacy_record()  # idempotent; a no-op once the legacy file is gone
    try:
        loaded = _load_record_file(path, dir_fd)
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        raise OSError(f"autonudge trust record unreadable: {path}") from exc
    if loaded is None:
        raise OSError(f"autonudge trust record malformed: {path}")
    raw, entries = loaded
    if not _is_sealed(raw, entries):
        logger.warning("autonudge trust record carries no valid seal; treating as empty")
        return {}
    return {str(loop_id): entry for loop_id, entry in entries.items()}


def _write_record(entries: dict[str, Any], dir_fd: int) -> None:
    """Publish *entries* sealed, through the held record directory.

    Only ever called inside :func:`_record_lock`, whose *dir_fd* is the record's
    directory held open since before the transaction's first read. Where the
    platform can, the staged temp is created and the publishing rename performed
    RELATIVE to it (``atomic_write(parent_dir_fd=...)``), so the write lands in the
    directory that was proved and read from, whatever the name resolves to by now;
    elsewhere the held handles keep that name bound for the by-name floor.
    """
    path = self_arm_record_path()
    atomic_write(
        path,
        json.dumps(
            {"version": 2, "loops": entries, "seal": _seal(entries)},
            ensure_ascii=False,
            sort_keys=True,
        ),
        fsync=True,
        parent_dir_fd=dir_fd if _descriptor_relative() else None,
    )


def record_self_arm(loop_id: str, slot_key: str) -> None:
    """Record that *loop_id* on *slot_key* was armed by that session's own turn.

    A pure UPSERT of one entry: every other entry is preserved verbatim. The
    write deliberately does NOT prune against a "live loop ids" set supplied
    by the caller -- the authorizer takes that snapshot outside this lock, and
    two crew/member sessions arming in the same second (the crew-boot case the
    exception exists for) would race it: the arm whose snapshot predates the
    other's ``svc.add`` but acquires the lock LAST would prune the sibling's
    freshly written entry, and that sibling's loop -- reported as armed --
    would be refused at every fire. Removing entries is revocation's job
    (:func:`forget_self_arm`), reached from every path a loop leaves the store
    (``AutoNudgeService.remove_sync``), so the record cannot grow past the
    loops that were ever armed and not yet removed. Raises ``OSError`` on a
    failed write: the caller (the authorizer) treats that as fail-closed -- a
    self-armed loop that cannot be recorded would never be allowed to fire, so
    it must not be reported as armed.
    """
    _record_arm(loop_id, slot_key, ARMED_BY_SELF)


def record_owner_arm(loop_id: str, slot_key: str, *, txn: str = "") -> None:
    """Record that *loop_id* on *slot_key* was armed by the dashboard OWNER.

    The owner's Perpetual mode switch on the Crew Members page arms a member's
    own thread from OUTSIDE that thread's turn, which the self-arm exception
    does not cover. It is admitted on the owner-gated member route only, and
    recorded here under its own ``armed_by`` so the fire-time guard can vouch
    for it without the loop ever claiming to be self-armed: a self-arm entry
    never satisfies :func:`is_recorded_owner_arm` and an owner entry never
    satisfies :func:`is_recorded_self_arm`. Same lock, same upsert-only write,
    same revocation path (``forget_self_arm`` on removal) and the same
    fail-closed ``OSError`` contract as :func:`record_self_arm`.

    PRECEDENCE: one entry per loop, one party per entry. An owner TAKEOVER of
    a loop the member armed itself (the owner turning Perpetual mode on over
    a stopped self-armed loop) rewrites that loop's entry to ``owner``; the
    loop is then governed by the owner's rules (caps refused to the member,
    its stop retained) and the store's ``self_armed`` bit, which nothing
    rewrites, is inert because :func:`is_recorded_self_arm` does not vouch
    for an owner entry. The caller that takes over reads the party first
    (:func:`read_arm_party_strict`) so a failed takeover can put it back,
    and stamps its own ``txn`` token on the entry so that restore touches
    only the entry IT wrote: "still says owner" is not an identity once a
    second takeover can write owner too. An arm-time owner entry (no
    takeover) carries no token.
    """
    _record_arm(loop_id, slot_key, ARMED_BY_OWNER, txn=txn)


def _record_arm(loop_id: str, slot_key: str, armed_by: str, *, txn: str = "") -> None:
    with _record_lock() as dir_fd:
        # Strict: a corrupt file refuses the write (OSError, fail closed for
        # the arm) rather than being replaced by a map holding only this entry.
        entries = _read_record_strict_raw(dir_fd)
        entry: dict[str, Any] = {
            "slot_key": str(slot_key),
            "armed_ts": time.time(),
            "armed_by": armed_by,
        }
        if txn:
            entry["txn"] = str(txn)
        entries[str(loop_id)] = entry
        _write_record(entries, dir_fd)


def restore_arm_party_if_token(
    loop_id: str, slot_key: str, expected_txn: str, prior_party: str
) -> bool:
    """Undo one takeover's owner entry, atomically, and only if it is still that takeover's.

    One locked read-modify-write: under :func:`_record_lock` the record is read
    strictly and verbatim, the entry for *loop_id* must name *slot_key*, say
    ``owner`` and carry exactly *expected_txn*; then it is rewritten to a self
    entry (``prior_party == "self"``) or removed (``prior_party == ""``), and
    every sibling is written back byte-for-byte. Returns ``True`` when it
    restored, ``False`` when the entry is not this takeover's any more (a later
    takeover's token, another party, no entry) -- in which case nothing is
    written. Because the compare and the write share the lock, an entry cannot
    change between them; the caller never needs a second read. A
    ``prior_party`` of ``owner`` or a blank token means nothing was changed and
    nothing is restored (``False``). Raises ``OSError`` when the file or the
    entry is malformed (fail closed, file left intact) or the write fails.
    """
    if not expected_txn or prior_party == ARMED_BY_OWNER:
        return False
    if prior_party not in (ARMED_BY_SELF, ""):
        raise ValueError(f"unknown prior party {prior_party!r}")
    with _record_lock() as dir_fd:
        entries = _read_record_strict_raw(dir_fd)
        entry = entries.get(str(loop_id))
        if entry is None:
            return False
        if not isinstance(entry, dict) or not isinstance(entry.get("slot_key"), str):
            raise OSError(f"autonudge trust record entry for {loop_id} is malformed")
        if entry["slot_key"] != str(slot_key) or entry.get("armed_by") != ARMED_BY_OWNER:
            return False
        if entry.get("txn") != expected_txn:
            return False
        if prior_party == ARMED_BY_SELF:
            entries[str(loop_id)] = {
                "slot_key": str(slot_key),
                "armed_ts": time.time(),
                "armed_by": ARMED_BY_SELF,
            }
        else:
            del entries[str(loop_id)]
        _write_record(entries, dir_fd)
        return True


def forget_self_arm(loop_id: str) -> None:
    """Drop *loop_id* from the record. Best-effort; never raises."""
    try:
        revoke_arm(loop_id)
    except OSError:
        logger.warning("could not revoke self-arm record for %s", loop_id, exc_info=True)


def revoke_arm(loop_id: str) -> None:
    """Drop *loop_id* from the record, whichever party wrote it. STRICT: an
    unreadable or unwritable record raises ``OSError`` so the caller can report
    that the authorization is still standing. Siblings are kept verbatim.

    The entry is the whole of an owner arm's fire-time authorization (there is
    no store bit beside it), so a loop the owner turned OFF must lose it: the
    loop store is agent-writable, and a retained entry would let a forged
    ``active: true`` on the paused record resume wakes the owner stopped. The
    owner's next ON records a fresh entry through the takeover path.
    """
    with _record_lock() as dir_fd:
        entries = _read_record_strict_raw(dir_fd)
        if str(loop_id) in entries:
            del entries[str(loop_id)]
            _write_record(entries, dir_fd)


def is_recorded_self_arm(loop_id: str, slot_key: str) -> bool:
    """Whether the trust record vouches that *loop_id* self-armed on *slot_key*.

    Total: any failure to read is ``False`` (refuse). Both the id AND the slot
    must match, so a forged loop that reuses a recorded id on a different slot
    does not inherit the authorization.
    """
    return _armed_by_of(loop_id, slot_key) == ARMED_BY_SELF


def is_recorded_owner_arm(loop_id: str, slot_key: str) -> bool:
    """Whether the trust record vouches that the OWNER armed *loop_id* on *slot_key*.

    Total like :func:`is_recorded_self_arm`, and disjoint from it: an entry
    vouches for exactly one arming party.
    """
    return _armed_by_of(loop_id, slot_key) == ARMED_BY_OWNER


def read_arm_party_strict(loop_id: str, slot_key: str) -> str:
    """The recorded arming party for *loop_id* on *slot_key* -- ``"self"``,
    ``"owner"`` or ``""`` for none -- RAISING ``OSError`` when the record
    exists and cannot be read, instead of answering ``""``.

    For the callers whose refusing answer is not the safe one: an applier
    deciding whether a member may cap or remove its own loop must fail CLOSED
    on an unreadable record (refuse the cap, keep the record), and a total
    read would hand it the permissive ``""`` there. So every INDETERMINATE
    state raises: an unreadable or mis-shaped file, and an entry for this loop
    that is present but malformed (not a dict, no string ``slot_key``, an
    ``armed_by`` that names no known party, a non-string takeover token). Only
    two states answer ``""``, and both are certain: no file at all, or no entry
    for this loop -- nothing was ever recorded. An entry naming ANOTHER slot is
    a certain answer too: whoever armed that loop did not arm it on this slot.
    """
    entries = _read_record_strict_raw()
    if str(loop_id) not in entries:
        return ""
    entry = entries[str(loop_id)]
    if not isinstance(entry, dict) or not isinstance(entry.get("slot_key"), str):
        raise OSError(f"autonudge trust record entry for {loop_id} is malformed")
    if entry["slot_key"] != str(slot_key):
        return ""
    armed_by = entry.get("armed_by", ARMED_BY_SELF)
    if armed_by not in (ARMED_BY_SELF, ARMED_BY_OWNER):
        raise OSError(f"autonudge trust record entry for {loop_id} names an unknown party")
    if not isinstance(entry.get("txn", ""), str):
        raise OSError(f"autonudge trust record entry for {loop_id} carries a malformed token")
    return str(armed_by)


def _armed_by_of(loop_id: str, slot_key: str) -> str:
    """The recorded arming party for *loop_id* on *slot_key*, or ``""``.

    An entry with no ``armed_by`` predates the field and was written by the
    only writer that existed then -- the self-arm path -- so it reads as
    ``"self"``. Any other spelling reads as nobody, which refuses.
    """
    entry = _read_record().get(str(loop_id))
    return _party_of_entry(entry, slot_key)


def _party_of_entry(entry: dict[str, Any] | None, slot_key: str) -> str:
    if entry is None or entry.get("slot_key") != str(slot_key):
        return ""
    armed_by = entry.get("armed_by", ARMED_BY_SELF)
    if armed_by in (ARMED_BY_SELF, ARMED_BY_OWNER):
        return str(armed_by)
    return ""


def recorded_arm_parties() -> dict[tuple[str, str], str]:
    """All sealed arm parties, read and verified once.

    The members roster projects every crewmate in one response. Reading the
    same sealed file once per row would block the event loop and repeat its
    JSON parse and HMAC check. This total, fail-closed snapshot lets that
    caller offload one read, then use only in-memory lookups per row.
    """
    parties: dict[tuple[str, str], str] = {}
    for loop_id, entry in _read_record().items():
        if not isinstance(entry, dict):
            continue
        slot_key = entry.get("slot_key")
        if not isinstance(slot_key, str):
            continue
        party = _party_of_entry(entry, slot_key)
        if party:
            parties[(str(loop_id), slot_key)] = party
    return parties


async def await_thread_to_completion(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """``asyncio.to_thread`` that a cancellation cannot leave running unjoined.

    For the trust-record writes (``record_owner_arm``, the takeover restore):
    the thread is started as its own future and awaited through a shield; if
    the awaiting task is cancelled, the thread is awaited AGAIN -- shielded,
    in a loop, so a SECOND cancellation does not abandon it either -- until
    the thread future is done, and only then does the cancellation propagate.
    By the time the caller unwinds (releasing a lock, letting a shutdown
    drain's join report done) the write has finished. The wait is bounded by
    the write itself (one locked file rewrite) and, at shutdown, by the
    drain's own join timeout, which gives up on the TASK and logs so; the
    thread still runs to its end on its own. The thread's own exception, when
    the caller was cancelled, is dropped here: the caller is unwinding on the
    cancel, and the write's outcome is re-read by whoever acts next.
    """
    fut = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(fut)
    except asyncio.CancelledError:
        while not fut.done():
            try:
                await asyncio.shield(fut)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - the write's own error is not this cancel's
                break
        raise


#: PER-SLOT LOCK for the owner's Perpetual mode on one member slot. Every
#: transition that pairs the arm record with the loop's state takes it: the
#: switch's mutations in ``handlers.members`` (identity re-check, loop re-read,
#: takeover, OFF's pause + revoke), the member's own ``autonudge_stop`` /
#: ``monitor_stop`` and its ``monitor_update`` cap check in
#: ``session_directive_apply``, AND the fire path's admission on a member slot
#: (``gateway._dashboard_mode_admits`` through turn publication). Without the
#: last one the timer could read the owner entry, lose the CPU to an OFF that
#: pauses the loop and revokes the entry, then publish a wake on a loop whose
#: authorization is gone; without the cap one the member could read "self-armed",
#: lose the CPU to a takeover that resumes the loop uncapped, then write a
#: finite cap onto the owner's loop. ``monitor_update`` of non-cap fields and
#: the service's own bookkeeping go through the nudge service's locks, not this
#: one. Process-local like the service. Entries are dropped once no holder or
#: waiter remains (``_PERPETUAL_LOCK_USERS``), so the map is bounded by the
#: operations in flight, not by the members ever touched. Lives HERE, next to
#: the record, because the gateway, the handler and the directive consumer all
#: import this leaf and none of them may import each other for it.
_PERPETUAL_LOCKS: dict[str, asyncio.Lock] = {}
_PERPETUAL_LOCK_USERS: dict[str, int] = {}


class perpetual_slot_lock:
    """``async with perpetual_slot_lock(slot_key):`` -- the per-slot lock, refcounted."""

    def __init__(self, slot_key: str) -> None:
        self._key = slot_key
        self._lock: asyncio.Lock | None = None

    async def __aenter__(self) -> None:
        lock = _PERPETUAL_LOCKS.get(self._key)
        if lock is None:
            lock = _PERPETUAL_LOCKS[self._key] = asyncio.Lock()
        _PERPETUAL_LOCK_USERS[self._key] = _PERPETUAL_LOCK_USERS.get(self._key, 0) + 1
        self._lock = lock
        try:
            await lock.acquire()
        except BaseException:
            self._release_use()
            raise

    async def __aexit__(self, *_exc: object) -> None:
        assert self._lock is not None
        self._lock.release()
        self._release_use()

    def _release_use(self) -> None:
        left = _PERPETUAL_LOCK_USERS.get(self._key, 1) - 1
        if left <= 0:
            _PERPETUAL_LOCK_USERS.pop(self._key, None)
            _PERPETUAL_LOCKS.pop(self._key, None)
        else:
            _PERPETUAL_LOCK_USERS[self._key] = left
