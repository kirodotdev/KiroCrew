"""The terminal conversation export the sessions archive carries.

**Terminal conversations (``conversations/`` root).** The two transcript halves
of the sessions archive are the GATEWAY's session state; the terminal (kiro-cli itself) keeps its
own conversations in ``conversations`` and ``conversations_v2`` inside
``~/.local/share/kiro-cli/data.sqlite3``, a store disjoint from both halves.
That file is ALSO the identity auth store -- ``hooks.py`` classifies it as a
token path and it holds live bearer tokens -- so the archive
must never carry the file. It carries a table-scoped export instead: a fresh
database holding ONLY the tables in ``_CONVERSATION_TABLES`` (an allowlist, so no
identity or token TABLE can leak even if kiro-cli adds one -- the bound is per
table and not per column, see ``_copy_table``), read from the live
store under a single read-only snapshot as ``_export_cli_conversations``
documents, under
the ``conversations/`` archive root beside ``crew`` and ``cli``. The store is
looked up among FIXED, home-anchored locations only: ``XDG_DATA_HOME`` /
``LOCALAPPDATA`` are not consulted, because the fence that keeps agent file tools
out of this store is home-anchored and does not follow a redirected root, and this
archive is uploaded off-host unattended. A host that relocates its store therefore
gets no ``conversations/`` root, and the run record says so through
``conversations_skipped`` rather than leaving an operator to infer it from an
absent member. The DECLARED
BOUNDARY of "conversation state" for this app is exactly the terminal's two chat
tables, ``conversations`` and ``conversations_v2``; a store that has not migrated
holds its rows in the first and a migrated one holds them in the second, so
carrying only one of them would leave an un-migrated install's conversations
behind while the run recorded a store with no conversation table at all.
If a future table is genuinely conversation state and not auth, it is added to
``_CONVERSATION_TABLES`` and this sentence is updated in the same change --
there is no other place the boundary is expressed. The export rides the SAME
standing permission as the ``cli`` half, :func:`sessions_layer_b_enabled`, and
invents no new grant: both carry what a model actually held, where
the crew transcript carries what was displayed with display-time redaction
applied. Neither is shipped raw: every text column of an exported conversation row
goes through ``_redacted_row`` first, so what leaves the host is
EGRESS-REDACTED, so an install whose operator withholds Layer B gets an archive with no
``conversations/`` root either. Reading the store is RECORDED through the
sanctioned credential-read audit
(``hooks.emit_internal_read_audit`` under ``aws_control.conversation_export``,
registered in ``hooks._AUDIT_ONLY_READ_IDS``), which runs after the read rather
than gating it -- it is an access log, not an authorization. What FAILS CLOSED is
the SHIPPING: an export whose
access cannot be recorded is dropped from the archive rather than shipped
unaudited, because the file holds live bearer tokens whatever this reader
touches.

The export runs inside ``backup.run_sessions_backup``, which writes the archive this
module adds members to; nothing here reaches the drive.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import stat
import sys
import tarfile
import tempfile
import urllib.parse
from pathlib import Path
from typing import IO, Any, NamedTuple

from kiro_crew import hooks

# kiro-cli's data.sqlite3 is also read in this process through the shim (kiro_cli,
# kiro_prerequisite). One SQLite library keeps the export's read lock when one of
# those connections closes; a second library's close would drop it.
from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.apps.builtins.aws_control.backend.backup_parts import _FACADE_MODULE
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.egress_text import (
    _redact_egress,
)
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.state import APP_NAME
from kiro_crew.apps.builtins.aws_control.backend.backup_parts.traversal import (
    _CAN_PIN_TRAVERSAL,
    _O_DIRECTORY,
    _O_NOFOLLOW,
    _O_NONBLOCK,
)
from kiro_crew.apps.manager import app_data_dir
from kiro_crew.identity_stores import _store_write_time, state_db_candidates
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.platform_compat import first_linked_ancestor, is_link_or_junction

logger = logging.getLogger(_FACADE_MODULE)


#: The exact tables the conversation export carries out of the kiro-cli store,
#: and the ONLY ones. Everything else in ``data.sqlite3`` -- every identity /
#: token / usage table ``hooks.py`` classifies as an auth store, whatever its
#: name -- is left behind by construction: the export writes THIS allowlist and
#: nothing else, so the archive can never carry a byte of a table outside it, even
#: if kiro-cli adds a new credential TABLE tomorrow. A denylist would fail OPEN the
#: day such a table appeared; an allowlist fails closed. The bound is per table and
#: not per column: every column an allowlisted table declares is copied, so a
#: credential column added to one of THESE tables would ride -- see
#: :func:`_copy_table`.
#:
#: ``conversations`` and ``conversations_v2`` are the terminal's own chat stores --
#: the un-migrated and migrated shapes of the same data. A store carries whichever
#: its install has reached, and on a store that has migrated the older table is
#: present and empty, so carrying both costs nothing there and is the only way an
#: install that never migrated has its conversations exported at all. Carrying just
#: one also makes the ``no_conversation_table`` outcome untrue on the other kind of
#: store: the rows exist, they are simply outside the allowlist, and the run record
#: would report an absence rather than a coverage gap. The
#: boundary of what counts as "conversation state" is declared in this module's
#: header; widening this tuple is the one change that widens that boundary, so it
#: is the single place a reviewer looks.
_CONVERSATION_TABLES: tuple[str, ...] = ("conversations", "conversations_v2")


#: The archive member names for the conversation export. The database rides under
#: its own ``conversations/`` root (a third root beside ``crew`` and ``cli``), and
#: the manifest beside it records the table set and per-table row count so an
#: archive that carried the conversations is distinguishable from one that did
#: not, and a silently-empty export is caught by comparing these counts to source.
_CONVERSATIONS_ARC_PREFIX = "conversations"


_CONVERSATIONS_DB_ARCNAME = f"{_CONVERSATIONS_ARC_PREFIX}/conversations.sqlite3"


_CONVERSATIONS_MANIFEST_ARCNAME = f"{_CONVERSATIONS_ARC_PREFIX}/CONVERSATIONS_MANIFEST.json"


#: The sanctioned credential-read audit id for opening the kiro-cli store. The
#: store holds live bearer tokens, so every reader owes an SEL trail; this id is
#: registered in ``hooks._AUDIT_ONLY_READ_IDS`` and the export fails closed if the
#: audit cannot be recorded. The registry holds its own literal, so this constant
#: does not make the two strings one -- what catches a drift is
#: ``test_the_conversation_read_id_is_registered_in_hooks``, which asserts this
#: value is present there. An unregistered id fails every read closed, so a drift
#: is loud rather than silent, but it is the test that keeps them equal.
_CONVERSATION_READ_ID = "aws_control.conversation_export"


#: The per-cell byte ceiling for a copied conversation value, and the number of rows
#: the copy fetches at a time. Together they are the export's peak-memory bound: at
#: most ``_CONVERSATION_BATCH_ROWS * _CONVERSATION_MAX_CELL_BYTES`` of conversation
#: text is live at once. A row count ALONE bounds nothing, because one field can be
#: arbitrarily wide, and an allocation failure here would take the whole sessions
#: backup down with it rather than costing only this sub-member.
#:
#: Both numbers come from a live store rather than a guess: its widest
#: ``conversations_v2`` value measures 2.5 MiB, and the table totals 1.1 GiB across
#: 3226 rows, so a 500-row batch of real data is roughly 180 MiB. The ceiling sits
#: well above the widest real value so an ordinary host is never refused, and the
#: batch is small because an average row here is hundreds of kilobytes.
_CONVERSATION_MAX_CELL_BYTES = 16 * 1024 * 1024


_CONVERSATION_BATCH_ROWS = 4


def _kiro_cli_conversation_db() -> tuple[Path | None, str]:
    r"""This host's kiro-cli store, and why there is none when there is none.

    Resolved through :func:`identity_stores.state_db_candidates` against FIXED,
    home-anchored locations. The environment is deliberately NOT consulted -- not
    ``XDG_DATA_HOME`` on POSIX, not ``LOCALAPPDATA`` or ``APPDATA`` on Windows --
    which is why an empty mapping is passed rather than ``os.environ``.

    **Why a relocated store is skipped rather than found.** The fence that makes
    this store unreadable and unwritable by agent file tools is home-anchored:
    ``security.paths`` splices in :func:`identity_stores.fenced_home_dirs`, and its
    own comment records that "a profile redirected outside the home directory is
    not covered". So a store re-rooted by one of those variables sits OUTSIDE the
    fence, where an agent can author rows. This function feeds an archive that is
    uploaded off-host unattended, so honouring the variable would let an agent
    plant rows in a ``conversations_v2`` table at a location it may write and have
    a scheduled backup ship them. An uploaded object cannot be un-sent.
    :func:`kiro_prerequisite` records the same decision for the same two variables
    and states the rule this follows: a fixed anchor cannot be pointed at
    something the agent may write. Being wrong in this direction costs a
    relocated-store install its terminal conversations, which the run record shows
    as an absent ``conversations/`` root; being wrong in the other direction
    uploads agent-authored content and cannot be undone.

    The candidates come back current-platform, most likely first, deduped; the
    first that is a regular file reached through no redirection is this host's.

    **The second return value says WHY no store was used, and it is never empty when
    none was.** It is a reason string, and the caller suppresses the retention sweep on
    any reason, so each of these cases keeps an earlier archive alive rather than
    letting this run retire it. Three cases reach it, and none is exotic:
    ``store_rejected_link`` for a candidate refused by either redirection test -- and
    the ancestor walk rejects on ANY parent from ``/`` down, including the home
    directory, so an ordinary symlinked ``~/.local/share`` or a symlinked home takes
    this exit permanently -- ``store_unreadable`` for one whose stat raised ``OSError``,
    and ``store_absent`` when nothing is at any fenced location.

    Absence reports a reason for a reason worth stating, since the opposite reads as
    obvious: the question the caller asks is whether an EARLIER archive holds rows this
    one does not, which is about backup history rather than about what is on disk now. A
    store wiped to clear corruption, removed by a reinstall, or on a volume not mounted
    at nightly-run time was present when last week's archive was written. Nothing pins a
    store's presence from one run to the next, so a per-run observation cannot answer
    the question, and answering it optimistically erases the last archive that held the
    conversations.

    **The ORDER of the two redirection tests and the file test is the guard, not a
    detail.** ``is_file()`` on a LOCAL-looking path whose ANCESTOR is a junction to
    ``\\host\share`` opens an outbound SMB connection that authenticates as this
    process, and it does so inside the stat itself -- before any check of ours can
    reject anything. So the ancestor walk runs FIRST, on every candidate, before
    the path is stat-ed at all. :func:`platform_compat.first_linked_ancestor` tests
    ancestors root-first and stops at the first link, so the walk never traverses
    one either. It deliberately excludes the leaf, which is why
    :func:`platform_compat.is_link_or_junction` still tests the candidate itself:
    ``islink`` alone answers False for a Windows junction, so a bare symlink check
    would accept a junction and read the store it points at instead of this host's.
    A fixed anchor bounds where a candidate may live; it does not stop a link
    planted AT that anchor from redirecting the read, so both guards are needed.
    """
    # Fixed home-anchored candidates only -- an empty mapping, never `os.environ`.
    # See the relocation note above: a redirected root falls outside the
    # agent-file-tool fence, and this feeds an off-host upload.
    candidates = state_db_candidates(sys.platform, Path.home(), {})
    # Why a candidate was declined, for the caller. The FIRST decline is kept rather
    # than the last: candidates come back most-likely-first, so the earliest one is
    # the store this host would have used.
    declined = ""
    # Every candidate that clears both redirection tests AND is a regular file, not
    # just the first. On Windows the table lists Local (the current layout) before
    # Roaming (legacy), so returning the first would let a leftover in the abandoned
    # root mask the live account. `identity_stores.selected_store` already arbitrates
    # that exact state by write time; this reuses its READING rather than its answer,
    # because that function stats its candidates itself, before anything has checked
    # them for redirection -- wrapping it would put the outbound stat back ahead of
    # the guard, which is the hole the ordering above exists to close.
    #
    # `_store_write_time` is imported rather than reimplemented even though it is that
    # module's private name. Two copies of "newest write across the main file and its
    # WAL sidecar" can drift, and the cost of drift here is exporting a stale store
    # while reporting success, silently; a rename instead breaks the import loudly, at
    # import time, under mypy and every test that loads this module.
    cleared: list[Path] = []
    for db in candidates:
        try:
            # Redirection tests BEFORE `is_file()`. See the order note above: the
            # stat is the outbound connection, so it must not run on a path this
            # has not already cleared.
            if first_linked_ancestor(db) or is_link_or_junction(db):
                declined = declined or "store_rejected_link"
                continue
            if db.is_file():
                cleared.append(db)
        except OSError:
            declined = declined or "store_unreadable"
            continue
    if cleared:
        try:
            # `max` keeps the FIRST maximal element, so equal write times prefer the
            # earlier table row -- Local, the current layout -- exactly as
            # `selected_store` resolves a tie. `_store_write_time` also reads the
            # `-wal` sidecar, because a commit lands there and the main file's mtime
            # does not advance until a checkpoint, so the main file alone
            # under-reports recency on the very store being written.
            return max(cleared, key=_store_write_time), ""
        except OSError:
            # A store vanished between the check above and the stat. Fall back to the
            # current-layout row rather than losing the export, which is what
            # `selected_store` does when a write time cannot be read.
            return cleared[0], ""
    # Absence is reported too, and is NOT the reasonless case it looks like. The
    # question the caller asks this field is "could an older archive hold
    # conversations this one does not", which is about backup HISTORY, not about
    # whether a store is here now. A store wiped to clear corruption, removed by a
    # reinstall, or sitting on an unmounted volume at nightly-run time was present
    # last week, so last week's archive holds rows this run cannot carry. Nothing
    # pins a store's presence across runs, which is the same reason the relocation
    # flag could not be treated as standing: a per-run observation cannot answer a
    # question about earlier archives.
    return None, declined or "store_absent"


def _store_relocated_outside_the_fence() -> bool:
    """Whether this host's environment re-roots the store away from the fenced set.

    :func:`_kiro_cli_conversation_db` deliberately reads only fixed, home-anchored
    candidates, so a relocated store is skipped. Skipping it SILENTLY is the
    failure this answers: an operator whose store lives outside home would believe
    an archive holds their terminal conversations when it holds none, and nothing in
    the run record would say otherwise.

    Compares the two candidate sets by PATH and touches the filesystem not at all --
    no ``stat``, no open, nothing. That is deliberate rather than incidental: the
    relocated root is outside the agent-file-tool fence, and probing a path there is
    the very thing the ancestor guard in :func:`_kiro_cli_conversation_db` exists to
    stop. A set difference needs no probe, so this reports the condition without
    reproducing the risk.

    True means "the environment names at least one store location this export will
    not read". It does NOT mean a store exists there; that question cannot be
    answered without a probe, and is not worth one. Reporting the relocation is
    enough for an operator to understand an absent ``conversations/`` root, which is
    the whole job.

    **Only a variable that MOVES a candidate counts, and that is the whole test.**
    ``LOCALAPPDATA`` and ``XDG_DATA_HOME`` re-root their own candidate, so the
    difference sees them. ``APPDATA`` does not, and is deliberately not compared: the
    Windows Roaming candidate is a fixed home anchor because
    :func:`identity_stores.state_db_candidates` does not follow that variable -- "the
    current generation writes the ``LOCALAPPDATA`` location, and the roaming default
    is retained only as a legacy fallback". A store kiro-cli does not write to cannot
    be relocated away from this export, so an ``APPDATA`` mismatch is not evidence of
    a relocation. Comparing it anyway reported one on any host with a redirected
    Roaming folder, which is an ordinary enterprise configuration, and that false
    positive froze retention permanently while a readable Local store sat beside it
    inside the fence. Nothing is lost by leaving it out: a legacy install that really
    does keep its store at a redirected Roaming root has no store at either fixed
    candidate, so the lookup reports ``store_absent`` and the sweep is suppressed on
    that path instead.
    """
    fixed = set(state_db_candidates(sys.platform, Path.home(), {}))
    relocatable = state_db_candidates(sys.platform, Path.home(), os.environ)
    return any(candidate not in fixed for candidate in relocatable)


class _ConversationExport(NamedTuple):
    """What one conversation export actually put in the archive.

    ``rows`` and ``members`` are tracked SEPARATELY because they answer different
    questions and genuinely disagree on a path this module takes: a present-but-
    empty allowlisted table IS carried, so a restore sees the real schema, and that
    archive holds two members and zero rows. Measuring content by rows alone reads
    such an archive as empty, and :func:`run_sessions_backup`'s "nothing to
    archive" guard would then discard members it had already written.

    ``rows`` feeds the archive's content count and the unchanged-run comparison.
    ``members`` answers only "is there a ``conversations/`` root in here".
    ``skipped`` names a reason the export carried less than the host holds, for the
    run record, and is empty when there is none. It exists because a silent skip is
    how an operator ends up believing they hold a backup they do not hold.

    **Any non-empty ``skipped`` also suppresses the retention sweep**, so setting it
    is not merely a reporting act -- see :func:`run_sessions_backup`. That is why the
    suppression is a predicate on this field rather than a list of qualifying reasons:
    a new reason added here cannot be forgotten from a predicate, and every reason
    this module emits qualifies anyway. Leave it EMPTY only when this run READ
    everything the host holds -- a successful export -- or when the operator has
    withheld the permission, which is a consented withdrawal rather than a gap. An
    absent store is NOT one of those: the question is whether an EARLIER archive holds
    rows this one does not, and a store that is missing now may have been present when
    that archive was written.
    """

    rows: int
    members: int
    skipped: str = ""


def _add_bytes(tar: tarfile.TarFile, payload: bytes, arcname: str) -> None:
    """Add ``payload`` to ``tar`` as ``arcname`` (mode 0600, deterministic)."""
    info = tarfile.TarInfo(name=arcname)
    info.size = len(payload)
    info.mode = 0o600
    info.mtime = 0
    info.type = tarfile.REGTYPE
    tar.addfile(info, io.BytesIO(payload))


class _ScratchExportUnsafe(Exception):
    """The scratch export this module wrote is not the file it wrote.

    Raised BEFORE the first tar write, so the caller reports a reason and carries
    nothing rather than shipping a member whose bytes came from somewhere else.
    """


def _conversation_scratch_parent() -> Path:
    """The agent-masked directory the conversation export is written under.

    This is the FIRST line of defence and it removes the attack rather than detecting
    it. A shared temp root cannot be made safe by descriptor pinning alone, because the
    pinning happens after a name the agent can already reach: a same-UID agent that
    replaces the temp DIRECTORY before this process opens it hands over a directory of
    its own, in which every pinned check passes on a file the attacker chose.

    ``app_data_dir(APP_NAME)`` is masked from agent sandboxes as a whole directory
    (``sandbox._CREW_HIDDEN_LEAVES`` carries ``apps/aws-control/data``), and it is the
    STRICTER of the two masked roots this app has: the sibling ``aws-control-staging``
    is deliberately granted to the AWS CLI spawn, and this scratch file is read only by
    this process, so it has no reason to be reachable from that child.

    Guarded exactly as :func:`restore_archive`'s staging is, and for the same reasons a
    per-file check cannot cover: a link planted AT the root would put the scratch file
    outside the fence wholesale, and ``exist_ok=True`` happily accepts a pre-existing
    link, so the resolve is re-checked after the ``mkdir`` rather than before it.
    """
    base = app_data_dir(APP_NAME)
    scratch = base / "conversations"
    if is_link_or_junction(scratch):
        raise ValueError("conversation scratch directory is not a real directory")
    # `mode=` at creation rather than a chmod afterwards: it leaves no instant in which
    # the directory exists group- or world-readable. umask can only clear bits, so the
    # result is never wider than 0700. Ignored on Windows, where the masked parent and
    # its own ACL are what restrict this.
    scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
    if scratch.resolve() != (base.resolve() / "conversations"):
        raise ValueError("conversation scratch directory resolves outside app storage")
    if not scratch.is_dir():
        raise ValueError("conversation scratch directory is not a real directory")
    return scratch


def _add_open_file(tar: tarfile.TarFile, fh: IO[bytes], size: int, arcname: str) -> None:
    """Add an ALREADY-OPEN, already-validated file to ``tar`` as ``arcname``.

    Takes the handle rather than a path because the caller's descriptor IS the
    authorization: re-deriving the file from its name here would reopen the swap
    window the caller just closed.
    """
    info = tarfile.TarInfo(name=arcname)
    info.size = size
    info.mode = 0o600
    info.mtime = 0
    info.type = tarfile.REGTYPE
    tar.addfile(info, fh)


def _open_pinned_scratch(dir_fd: int, name: str) -> tuple[IO[bytes], int]:
    """Open ``name`` under ``dir_fd`` and prove it is still the file we wrote.

    An earlier version of this read the scratch export BY PATH and stated that
    pinning was unnecessary "because the source is a file this process created under
    its own private ``TemporaryDirectory``". That justification was WRONG, and the way
    it was wrong is the reusable part: ``TemporaryDirectory`` is mode 0700, which
    excludes other USERS and not the same-UID agent this product's threat model
    assumes -- the one :mod:`kiro_crew.sandbox` describes planting links in the
    world-writable root. The directory was never private from the attacker that
    matters, so ``stat`` then ``open`` on a name left exactly the swap window
    :func:`_add_pinned` exists to close, and it paid out as a host file uploaded
    off-host with no recall.

    Three checks, each closing a different substitution:

    * ``O_NOFOLLOW`` -- the name may not resolve through a symlink.
    * ``S_ISREG`` on the DESCRIPTOR -- not a FIFO or device that would make the read
      block or return a stream that is not the export.
    * ``st_nlink == 1`` -- a hard link defeats the other two by construction, because
      the target is a genuine regular file reached under our own name.

    The size comes from the same ``fstat`` as the checks, so the header cannot
    describe one file while the body streams another.

    Reachable only through :func:`run_sessions_backup`, which refuses outright on a
    platform without descriptor pinning, so these flags are real here and never the
    ``getattr`` zero fallback.
    """
    fd = os.open(name, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK, dir_fd=dir_fd)
    handle = open(fd, "rb", closefd=True)
    try:
        st = os.fstat(handle.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise _ScratchExportUnsafe("the scratch export is not a regular file")
        if st.st_nlink != 1:
            raise _ScratchExportUnsafe(f"the scratch export carries {st.st_nlink} links")
        return handle, st.st_size
    except BaseException:
        handle.close()
        raise


class _ConversationTooLarge(Exception):
    """One conversation field is wider than the ceiling, so nothing this export read ships.

    Its own exit rather than a folded-in ``store_unreadable``: the store was read
    fine and one field is pathological, which asks a different thing of the operator.
    The export carries nothing instead of dropping the row, for the reason
    :class:`_RedactionFailed` gives -- a partial copy produces an archive a restore
    reads as complete.
    """


class _RedactionFailed(Exception):
    """A redactor raised on a value, so nothing this export read may be shipped.

    Its own exit, not folded into ``store_unreadable``: the store WAS read here, and
    the two states need different reasons because they call for different operator
    action. Silently dropping the row would ship an archive a restore reads as
    complete, and falling back to the raw value would ship the credential this pass
    exists to remove -- so the export carries nothing and says why.
    """


def _redacted_row(row: tuple[Any, ...]) -> tuple[Any, ...]:
    """One source row with credentials and exfiltration URLs removed from its text.

    Only ``str`` values are rewritten. The allowlisted table's real schema is
    ``key``/``conversation_id``/``value`` TEXT plus two INTEGER timestamps, measured on
    a live store where every text column reports ``typeof() == 'text'``, so no BLOB
    carries conversation text and an integer has nothing to scrub.

    Raises :class:`_RedactionFailed` rather than returning the row: a caller that
    cannot scrub a value must not choose between dropping it and shipping it.
    """
    out: list[Any] = []
    for value in row:
        if not isinstance(value, str):
            out.append(value)
            continue
        try:
            cleaned = _redact_egress(value)
        except Exception as exc:  # noqa: BLE001 - any failure here means do not ship
            raise _RedactionFailed(str(exc)) from exc
        out.append(cleaned)
    return tuple(out)


def _refuse_an_oversized_cell(source: sqlite3.Connection, table: str, cols: list[str]) -> None:
    """Raise unless every cell of ``table`` fits :data:`_CONVERSATION_MAX_CELL_BYTES`.

    Measured in SQLite, in the caller's read transaction, BEFORE the first
    ``fetchmany``: ``length(cast(c as blob))`` yields a byte count without handing
    Python the value, so the ceiling is established rather than discovered by
    allocating. Being inside the snapshot is what makes one pass enough for the whole
    copy -- a writer committing a wider value mid-copy is outside it and cannot be
    read. One extra scan of the table is the price of a bound that precedes the fetch
    rather than following it.

    The message names the column and the byte count, never the value.
    """
    widest = ", ".join(f'max(length(cast("{c}" as blob)))' for c in cols)
    measured = source.execute(f'SELECT {widest} FROM "{table}"').fetchone() or ()
    for name, size in zip(cols, measured):
        if size is not None and size > _CONVERSATION_MAX_CELL_BYTES:
            raise _ConversationTooLarge(
                f"{table}.{name} holds a {size}-byte value, over the "
                f"{_CONVERSATION_MAX_CELL_BYTES}-byte ceiling"
            )


def _copy_table(source: sqlite3.Connection, target: sqlite3.Connection, table: str) -> int:
    """Copy every row of ONE allowlisted table into ``target``. Returns row count.

    The destination schema is taken from the source's own ``CREATE TABLE`` text
    (``sqlite_schema.sql``), and rows are moved through a named column list built
    from ``PRAGMA table_info`` rather than a literal ``SELECT *``. Be precise about
    what that buys: the list is derived from whatever columns the source declares
    at copy time, so it is NOT an allowlist and does NOT hold a column back. A
    column added to this table upstream -- including a credential-bearing one --
    is enumerated by the same ``PRAGMA`` and copied. What the named list gives is a
    stable, quoted column ORDER shared by the SELECT and the INSERT, so the copy
    cannot silently mis-align if the two ever saw different column sets. The
    fail-closed boundary is the TABLE allowlist in
    :data:`_CONVERSATION_TABLES`, one level up; column granularity is not
    implemented here.

    The table name is validated against the caller's allowlist before it reaches
    here, so it is never attacker-controlled; column identifiers are quoted
    defensively all the same.

    Peak memory is bounded by :data:`_CONVERSATION_BATCH_ROWS` rows of at most
    :data:`_CONVERSATION_MAX_CELL_BYTES` each, and a table holding a wider cell is
    refused outright rather than copied -- see :func:`_refuse_an_oversized_cell`.
    """
    create_sql = source.execute(
        "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not create_sql or not create_sql[0]:
        return 0
    target.execute(create_sql[0])
    cols = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")').fetchall()]
    if not cols:
        return 0
    col_list = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join("?" for _ in cols)
    count = 0
    _refuse_an_oversized_cell(source, table, cols)
    cursor = source.execute(f'SELECT {col_list} FROM "{table}"')
    insert = f'INSERT INTO "{table}" ({col_list}) VALUES ({placeholders})'
    while True:
        rows = cursor.fetchmany(_CONVERSATION_BATCH_ROWS)
        if not rows:
            break
        # A GENERATOR, not a list comprehension: the raw batch is already held, and
        # materialising the redacted copy beside it doubles the peak for the length of
        # the statement. ``executemany`` consumes one row at a time, so only one
        # redacted row is live at once.
        target.executemany(insert, (_redacted_row(row) for row in rows))
        count += len(rows)
    return count


def _export_cli_conversations(tar: tarfile.TarFile) -> _ConversationExport:
    """Export ONLY the terminal conversation tables into ``tar``. Returns row count.

    ``data.sqlite3`` is BOTH the terminal's conversation store and its identity
    auth store: ``hooks.py`` classifies the file as a token path and it
    holds live bearer tokens. Tar-ing the file would upload live
    credentials off-host, strictly worse than the gap this closes. So this reads
    the source and writes a FRESH database holding only :data:`_CONVERSATION_TABLES`
    -- an allowlist, so no byte of any table OUTSIDE it (no identity row, no token
    column of an auth table) can reach the archive even if kiro-cli adds a
    credential table later. The bound is per TABLE, not per column: every column an
    allowlisted table declares is copied, so a credential column added to
    ``conversations_v2`` itself would ride. See :func:`_copy_table`.

    **The source is a LIVE WAL-mode database, so a consistent read needs care.**
    A commit lands in the ``-wal`` sidecar and folds into the main file only on a
    checkpoint, so the main file and its ``-wal`` only agree at an instant. The
    safe read does NOT copy those files, and does NOT checkpoint: a checkpoint is
    a WRITE, and this opens the operator's store ``mode=ro``, on which a
    ``wal_checkpoint`` cannot run. What a read-only connection DOES give is a
    consistent WAL-aware view -- SQLite applies the committed ``-wal`` frames
    transparently on read -- so the whole export runs inside ONE explicit read
    transaction (``BEGIN``), which pins a single snapshot for the life of the
    copy. Every allowlisted table is then read against that one snapshot, so a
    writer committing mid-copy cannot make two tables disagree. This is why
    copying the ``-wal`` and then the main file separately would be wrong: it
    takes them at two different times, and a WAL copied before its commit was
    checkpointed replays over newer pages so the archive restores BACKWARDS.

    Steps:

    1. Open the source READ-ONLY (``mode=ro``) so this writes no database page and
       runs no checkpoint against the operator's live store. It is not a promise of
       zero filesystem writes: on a WAL store SQLite may still update the ``-shm``
       shared-memory sidecar to take a read mark, which is how a reader
       participates in WAL at all. The store's own DATA is what is untouchable
       here. Open one deferred read transaction so all reads share a single
       consistent snapshot including committed WAL frames.
    2. Copy the allowlisted tables through a named per-table column list, which
       fixes a stable column order for the copy -- see :func:`_copy_table` for what
       that does and does not bound, since the list is derived from the source's
       declared columns and is not a column allowlist. The fail-closed boundary is
       the TABLE allowlist in :data:`_CONVERSATION_TABLES`.

    Best-effort at the STORE level (a missing store, an unreadable one, a store that
    cannot be resolved at all) -- those
    return an empty :class:`_ConversationExport` and the sessions backup proceeds
    with the transcript halves. NOT
    best-effort at the ROW level: once a readable store is found, every row of
    every allowlisted table is copied and counted, and the manifest's count is
    asserted against the source in the tests, so a partial copy fails loudly
    rather than shipping a short archive.

    **One exit deliberately does NOT report a skip: a failure while WRITING the
    members into the tar.** ``tarfile.addfile`` raising part-way leaves the archive in
    an undefined state, so swallowing it would upload a damaged tarball under a record
    saying the run succeeded -- worse than the failure it hides, and the one case where
    losing the whole archive is the correct outcome. Everything before the first tar
    write is guarded and reports; from the first tar write onward, an
    exception propagates.
    """
    # RELOCATION FIRST, before any lookup. A file may still sit at the fixed anchor
    # after the environment says the store moved -- a leftover from before the
    # relocation -- and looking there first would export those stale conversations
    # and report complete coverage, because the relocation would only be noticed
    # when the fixed lookup found nothing. `identity_stores.selected_store` arbitrates
    # this same "leftover in the abandoned root" state by mtime, so it is a state
    # this codebase already expects rather than a hypothetical. A store the
    # environment has moved away from is stale by definition, and exporting stale
    # conversations while recording complete coverage is worse than exporting
    # nothing and saying so.
    # Discovery is GUARDED, not because either call is expected to raise, but because
    # an exception escaping here would fail the whole sessions backup and throw away a
    # correct transcript archive over a missing sub-member. Both calls reach
    # ``Path.home()``, which raises when the home directory cannot be determined, and
    # the candidate walk touches the filesystem. This function's contract is
    # best-effort at the store level, so an unusable store must look the same however
    # it became unusable. ``RuntimeError`` is named for ``Path.home()`` specifically.
    try:
        relocated = _store_relocated_outside_the_fence()
        db, declined = (None, "") if relocated else _kiro_cli_conversation_db()
    except (OSError, RuntimeError) as exc:
        logger.warning(
            "aws-control: the kiro-cli conversation store could not be resolved, so no "
            "conversations were carried in this archive: %s",
            redact_log_via_context(str(exc)),
        )
        return _ConversationExport(0, 0, "store_discovery_failed")
    if relocated:
        logger.warning(
            "aws-control: the kiro-cli conversation export reads only fixed, "
            "home-anchored store locations, and this host's environment names a "
            "relocated one, so no conversations were carried in this archive"
        )
        return _ConversationExport(0, 0, "store_relocated_outside_fence")
    if db is None:
        # No store was USED, and every way of reaching that reports a reason. The
        # tempting exemption is absence: a host with no store looks like it has no
        # conversations for an older archive to hold. That asks about the store NOW,
        # while retention decides the fate of an archive written EARLIER -- a store wiped
        # to clear corruption, dropped by a reinstall, or on an unmounted volume was
        # present when that archive was written. An ordinary symlinked home reaches the
        # rejection case permanently, and either case erases the last complete archive
        # if it prunes, so both suppress.
        logger.warning(
            "aws-control: the kiro-cli conversation export used no store (%s), so no "
            "conversations were carried in this archive",
            declined,
        )
        return _ConversationExport(0, 0, declined)
    per_table: dict[str, int] = {}
    # Cut under the AGENT-MASKED app data root, not the system temp directory. That is
    # the defence; the descriptor pinning below is depth behind it. See
    # `_conversation_scratch_parent`.
    #
    # Its own failure is a REASON rather than an exception: a host whose data home
    # cannot hold a directory has not failed the backup, and the transcript halves are
    # already archived by the time this runs.
    try:
        scratch_parent = _conversation_scratch_parent()
    except (OSError, ValueError) as exc:
        logger.warning(
            "aws-control: kiro-cli conversation export dropped -- its masked scratch "
            "root was unusable, so nothing was carried: %s",
            redact_log_via_context(str(exc)),
        )
        return _ConversationExport(0, 0, "scratch_root_unusable")
    with tempfile.TemporaryDirectory(prefix="kc-conv-", dir=str(scratch_parent)) as tmp:
        dst = Path(tmp) / "conversations.sqlite3"
        src_uri = f"file:{urllib.parse.quote(str(db))}?mode=ro"
        try:
            with contextlib.closing(sqlite3.connect(src_uri, uri=True)) as source:
                # One consistent snapshot for the whole copy: a deferred read
                # transaction opened by the first SELECT holds a single point in
                # time, so a writer committing mid-copy cannot make two tables
                # disagree. No checkpoint (a write, impossible on mode=ro) and no
                # WAL refusal -- the read already sees committed WAL frames.
                source.execute("BEGIN")
                present = {
                    row[0]
                    for row in source.execute(
                        "SELECT name FROM sqlite_schema WHERE type='table'"
                    ).fetchall()
                }
                with contextlib.closing(sqlite3.connect(str(dst))) as target:
                    for table in _CONVERSATION_TABLES:
                        if table not in present:
                            # The allowlist names the terminal's un-migrated and
                            # migrated chat tables, and a store holds whichever its
                            # install has reached, so one of them being absent is an
                            # ordinary state. Skip it; do not fail the export.
                            continue
                        per_table[table] = _copy_table(source, target, table)
                    target.commit()
        except _RedactionFailed as exc:
            # The store was read and could not be SANITISED. Per the invariant this
            # module walks, that is a failed read rather than a policy decline, so it
            # carries a reason and suppresses the retention sweep. The audit records a
            # successful contact, because the read itself succeeded -- what failed is
            # the shipping, and conflating the two would misreport which half broke.
            hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "success")
            logger.warning(
                "aws-control: kiro-cli conversation export could not redact a value, so "
                "nothing was carried rather than shipping it unredacted: %s",
                redact_log_via_context(str(exc)),
            )
            return _ConversationExport(0, 0, "conversations_unredactable")
        except _ConversationTooLarge as exc:
            # Read fine, refused on width. Same shape as the redaction exit: the READ
            # succeeded, so the audit says so, and the reason suppresses the retention
            # sweep because an older archive may hold what this one does not.
            hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "success")
            logger.warning(
                "aws-control: kiro-cli conversation export refused a value wider than "
                "its per-cell ceiling, so nothing was carried: %s",
                redact_log_via_context(str(exc)),
            )
            return _ConversationExport(0, 0, "conversations_oversized")
        except MemoryError:
            # The bound above is meant to make this unreachable; it is caught anyway
            # because the alternative is losing a correct transcript archive over a
            # sub-member. Nothing is formatted into the message, since a handler for an
            # allocation failure should not ask for more memory.
            hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "success")
            logger.warning(
                "aws-control: kiro-cli conversation export ran out of memory, so it was "
                "left out of this archive and the rest of the backup continues"
            )
            return _ConversationExport(0, 0, "conversations_memory_exhausted")
        except (OSError, sqlite3.Error) as exc:
            # The store was opened (or the open failed) -- either way the contact
            # with a credential-bearing file owes a trail. Record it as unreadable
            # and carry nothing.
            hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "unreadable")
            logger.warning(
                "aws-control: kiro-cli conversation export could not read the store, so it "
                "was left out of this archive: %s",
                redact_log_via_context(str(exc)),
            )
            return _ConversationExport(0, 0, "store_unreadable")
        total = sum(per_table.values())
        if not per_table:
            # NO chat table existed at all -- neither the un-migrated nor the migrated
            # one -- so this reason means the terminal genuinely holds no conversations
            # rather than holding them in a table the allowlist omits. Nothing to carry,
            # and no empty member to add. (A present-but-empty table DOES get carried, so
            # a restore sees the real schema.) The store WAS opened, so the access
            # is still audited.
            hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "no_table")
            return _ConversationExport(0, 0, "no_conversation_table")
        # The store holds live bearer tokens, so opening it -- even to copy only
        # the conversation allowlist -- goes through the sanctioned credential-read
        # audit, and FAILS CLOSED: an export whose access cannot be recorded is
        # dropped from the archive rather than shipped unaudited. A logger line is
        # not an SEL audit.
        if not hooks.emit_internal_read_audit(_CONVERSATION_READ_ID, "success"):
            logger.warning(
                "aws-control: kiro-cli conversation export dropped -- its credential-read "
                "audit could not be recorded, so the conversations are left out of this "
                "archive rather than shipped unaudited"
            )
            return _ConversationExport(0, 0, "credential_audit_unavailable")
        added: list[str] = []
        # Open and VALIDATE before the first tar write, so a substituted scratch file
        # costs the conversations member and a reason -- not a damaged archive. The tar
        # write itself is deliberately outside this guard: per this function's contract,
        # everything before the first write reports and the write onward propagates.
        #
        # PLATFORM: the pinned read needs `openat`, and on a host without it the checks
        # have no equivalent worth inventing -- `os.open` cannot open a directory on
        # Windows, `O_NOFOLLOW` and `O_DIRECTORY` do not exist there, and `st_nlink` from
        # `fstat` is not a dependable link count. Rather than degrade to a weaker read,
        # this reports. It is unreachable in production: `run_sessions_backup` refuses
        # outright without `_CAN_PIN_TRAVERSAL` (`kind_unavailable_reason`), so the whole
        # sessions kind is already unavailable on such a host. The branch exists so a
        # DIRECT caller cannot quietly obtain the unpinned read the refusal exists to
        # prevent.
        if not _CAN_PIN_TRAVERSAL:
            logger.warning(
                "aws-control: kiro-cli conversation export dropped -- this platform has "
                "no descriptor-pinned open, so the scratch export cannot be read safely"
            )
            return _ConversationExport(0, 0, "scratch_pinning_unavailable")
        try:
            tmp_fd = os.open(tmp, os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
        except OSError as exc:
            logger.warning(
                "aws-control: kiro-cli conversation export dropped -- its scratch "
                "directory could not be pinned: %s",
                redact_log_via_context(str(exc)),
            )
            return _ConversationExport(0, 0, "scratch_export_unsafe")
        try:
            handle, size = _open_pinned_scratch(tmp_fd, dst.name)
        except (OSError, _ScratchExportUnsafe) as exc:
            logger.warning(
                "aws-control: kiro-cli conversation export dropped -- the scratch export "
                "was not the file this run wrote, so nothing was carried: %s",
                redact_log_via_context(str(exc)),
            )
            return _ConversationExport(0, 0, "scratch_export_unsafe")
        finally:
            os.close(tmp_fd)
        with handle:
            _add_open_file(tar, handle, size, _CONVERSATIONS_DB_ARCNAME)
        added.append(_CONVERSATIONS_DB_ARCNAME)
        manifest = json.dumps(
            {"tables": dict(sorted(per_table.items())), "total_rows": total},
            sort_keys=True,
        ).encode("utf-8")
        _add_bytes(tar, manifest, _CONVERSATIONS_MANIFEST_ARCNAME)
        added.append(_CONVERSATIONS_MANIFEST_ARCNAME)
    # Members are accumulated as they are written rather than asserted as a
    # constant, so the number the caller reads cannot drift from what this added.
    return _ConversationExport(total, len(added))
