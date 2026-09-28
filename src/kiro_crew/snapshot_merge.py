"""Merge restore: import a bundle into a live data home without overwriting what is there.

Additive by construction. Memory rows merge through an allowlisted SQL path, cron jobs by
name, notification records byte for byte, and trees copy in only what the destination
lacks. The merge driver, :func:`kiro_crew.snapshot._do_merge`, sequences these per
component. It skips a component whose data cannot be merged file by file, and refuses a
selection of nothing else (:data:`kiro_crew.snapshot_components._REPLACE_ONLY_COMPONENTS`);
replace restores those.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat as _stat
import tempfile
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.jsonl_util import RECORD_CAP, UndecodableRecord, UnreadableRecord
from kiro_crew.memory_stores import (
    MEMORY_STORES_DIR_NAME,
    is_host_local_store_state,
    memory_store_namespace_lock,
)
from kiro_crew.snapshot_archive import (
    _safe_name,
)
from kiro_crew.snapshot_components import (
    _facade,
    is_product_tree_database,
)

# Must match ``handlers_system._get_telemetry_salt`` (``secrets.token_bytes(32)``).
# ``_copy_locked`` loads telemetry_salt into memory, so a planted giant in an
# untrusted snapshot would OOM restore after earlier merge steps.
_TELEMETRY_SALT_BYTES = 32


def _copy_tree_no_overwrite(src: Path, dst: Path, *, allow_unpinned: bool = False) -> None:
    """Merge *src* into *dst* without overwriting, with both ends pinned.

    The destination side is the delicate one. Walking the source with ``rglob`` and
    writing each file with ``shutil.copy2`` to a path composed by name leaves the
    destination's ancestor chain unpinned: a component of *dst* swapped for a link after
    ``mkdir`` redirects the write, and ``not target.exists()`` answers for whatever the
    link points at rather than for the directory the caller validated.

    This is one call into the shared primitive with ``skip_existing=True``, which
    is what makes the no-overwrite promise real: exclusive creation is atomic, so "it
    did not exist a moment ago" and "this call created it" are the same statement
    rather than two with a window between them.

    An earlier revision open-coded a second pinned walk here, with its own copy body.
    Review pointed out the two had already diverged -- this one's child-directory open
    lacked the ``ELOOP``/``ENOTDIR`` handling, so the very swap the staging walk skips
    would have escaped restore as a raw ``OSError`` -- which is the argument for a
    parameter on one primitive rather than a parallel implementation the shared
    module's own docstring says should not exist.
    """
    facade = _facade()
    if not facade._staging_is_pinned(
        allow_unpinned=allow_unpinned, what=f"restore of {dst.name!r}"
    ):
        # Top-down with PRUNING, not `rglob("*")`. The distinction is the whole fence:
        # `rglob` yields a link's descendants whatever the loop body then does with the
        # link itself, so skipping the entry alone still copied the target's contents in
        # -- and the per-file `mkdir(parents=True)` below rebuilt the very directory the
        # skip meant to prune. `os.walk(topdown=True)` lets the walk be told not to
        # descend, by editing `dirnames` in place, so a pruned directory yields nothing.
        #
        # Both link predicates, as the sibling fallbacks in `snapshot_restore` and
        # `snapshot_archive` spell it: `is_symlink()` answers False for a Windows
        # directory junction, and Windows is exactly where this branch runs -- `dir_fd`
        # is missing there, so the pinned path is unavailable and an operator who passed
        # `--allow-unpinned-staging` lands here. The opt-in accepts a BY-NAME traversal,
        # which no screen here can make safe; it does not accept a link fence that sees
        # only half the links the platform has, nor one that prunes a name while the
        # walk keeps handing out what is behind it.
        def _is_link(path: Path) -> bool:
            return path.is_symlink() or pinned_fs.is_reparse_point(path)

        for parent, dirnames, filenames in os.walk(src, topdown=True):
            here = Path(parent)
            kept = [d for d in dirnames if not _is_link(here / d)]
            for pruned in [d for d in dirnames if d not in kept]:
                facade._report_skip(pinned_fs.SKIP_SYMLINK, str(here / pruned))
            dirnames[:] = kept
            for name in kept:
                (dst / (here / name).relative_to(src)).mkdir(parents=True, exist_ok=True)
            for name in filenames:
                item = here / name
                if _is_link(item):
                    facade._report_skip(pinned_fs.SKIP_SYMLINK, str(item))
                    continue
                target = dst / item.relative_to(src)
                target.parent.mkdir(parents=True, exist_ok=True)
                # `copy2` opens the destination BY NAME for writing, so a symlink planted
                # at that name after a `not target.exists()` check is followed and an
                # arbitrary external file is overwritten -- that `exists()` guard is itself
                # the name-based check that creates the window.
                #
                # copy_file_pinned opens the destination O_CREAT|O_EXCL|O_NOFOLLOW even
                # with no directory descriptor, so the link is refused rather than
                # followed, and O_EXCL subsumes the skip-if-present behaviour
                # `not target.exists()` provides -- without the race.
                pinned_fs.copy_file_pinned(
                    str(item), str(target), skip_existing=True, on_skip=facade._report_skip
                )
        return

    pinned_fs.stage_tree_pinned(
        src,
        dst,
        what=f"restore of {dst.name!r}",
        on_skip=facade._report_skip,
        skip_existing=True,
    )


_MERGE_ALLOWED_TABLES = frozenset(
    {
        "semantic_memory",
        "episodic_memories",
        "knowledge_facts",
        "knowledge_edges",
    }
)


_SAFE_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_identifier(name: str) -> str:
    """Validate a SQL identifier against allowlist pattern. Raises ValueError if invalid."""
    if not _SAFE_IDENTIFIER_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}")
    return name


def _merge_memory(src_db: Path, dst_db: Path) -> None:
    # Integrity check on source DB before ATTACH.
    #
    # `closing`, not a bare `with sqlite3.connect(...)`: a connection used as a context
    # manager commits or rolls back the TRANSACTION and leaves the connection OPEN. The
    # handle it kept on src_db made the caller's extraction temp dir undeletable on
    # Windows, which is how this surfaced.
    facade = _facade()
    try:
        with closing(facade.sqlite3.connect(str(src_db))) as check_conn:
            result = check_conn.execute("PRAGMA integrity_check;").fetchone()[0]
        if result != "ok":
            print(f"  ⚠️  Source DB integrity check failed: {result} — skipping merge")
            return
    except Exception as e:
        print(f"  ⚠️  Source DB unreadable: {e} — skipping merge")
        return

    conn = facade.sqlite3.connect(str(dst_db))
    conn.execute("BEGIN")
    attached = False
    try:
        conn.execute("ATTACH DATABASE ? AS src", (str(src_db),))
        attached = True
        for table, cols, where in [
            (
                "semantic_memory",
                "key, value_json, confidence, source, created_at, updated_at, embedding",
                "WHERE is_deleted=0",
            ),
            (
                "episodic_memories",
                "id, conversation_id, text, embedding, tags, importance, created_at, last_accessed_at",
                "WHERE is_deleted=0",
            ),
            ("knowledge_facts", "subject, predicate, object, episode_id, created_at", ""),
            (
                "knowledge_edges",
                "source_key, target_key, relation, weight, metadata, created_at",
                "",
            ),
        ]:
            if table not in _MERGE_ALLOWED_TABLES:
                raise ValueError(f"Table {table!r} not in merge allowlist")
            for col in cols.split(", "):
                _validate_identifier(col.strip())
            try:
                before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} ({cols}) "
                    f"SELECT {cols} FROM src.{table} {where}"
                )
                after = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                label = table.replace("_", " ").title()
                print(f"  {label} imported: {after - before}")
            except facade.sqlite3.OperationalError as e:
                import logging

                # The snapshot command's logger: every snapshot/restore diagnostic reports
                # under the one name, whichever module the code sits in.
                logging.getLogger("kiro_crew.snapshot").warning("Skipping table %s: %s", table, e)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if attached:
            try:
                conn.execute("DETACH DATABASE src")
            except Exception:
                pass
        conn.close()


def _usable_cron_shape(parsed: object, path: Path) -> bool:
    """Refuse a crons file that parsed but is not shaped like a cron file.

    The merge looks ``jobs`` up on the result and calls ``.get`` on every
    entry, so valid JSON of the wrong shape -- a top level that is not an
    object, a ``jobs`` that is not a list, a job that is not an object, or a
    present name that is not encodable text -- would raise TypeError,
    AttributeError, or UnicodeEncodeError a line or two further down: the
    same crash the read guard exists to prevent, just moved. Only the structure the merge itself relies on is
    checked; the fields of a job are the cron loader's business, not this
    one's. A missing ``jobs`` key keeps its existing meaning of "no jobs".
    """
    if not isinstance(parsed, dict):
        print(f"  ⚠️  {path} is not a cron file — skipping cron merge")
        return False
    jobs = parsed.get("jobs", [])
    if not isinstance(jobs, list) or not all(
        isinstance(job, dict)
        and (
            "name" not in job
            or (
                isinstance(job["name"], str)
                # json.loads accepts lone-surrogate escapes, and a present name
                # is UTF-8 encoded when the import id is hashed: a surrogate
                # would raise UnicodeEncodeError there, the same crash moved.
                and not any("\ud800" <= ch <= "\udfff" for ch in job["name"])
            )
        )
        for job in jobs
    ):
        print(f"  ⚠️  {path} has an unusable job list — skipping cron merge")
        return False
    return True


def _merge_crons(src_path: Path, dst_path: Path) -> bool:
    """Merge the archive's cron jobs into the live store.

    Returns ``True`` when the merged store was written, ``False`` when the
    merge was refused: an unreadable source, an unreadable destination, or an
    unusable cron shape on either side. The refusal diagnostics stay on
    stdout, but a print is invisible to a caller with no terminal — the
    dashboard import reported "crons (merged)" over a refusal that imported
    zero jobs — so the outcome is also returned for the caller to report.
    A failing WRITE of the merged store is not a refusal: the ``OSError``
    propagates, which is the loud behavior the caller's error path expects.
    """
    # Cron job names are operator-authored text and routinely non-ASCII, so the
    # locale codepage is the wrong decoder for this file on any host.
    try:
        src = json.loads(src_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ⚠️  Could not read {src_path}: {exc} — skipping cron merge")
        return False
    try:
        dst = json.loads(dst_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  ⚠️  Could not read {dst_path}: {exc} — skipping cron merge")
        return False
    if not _usable_cron_shape(src, src_path) or not _usable_cron_shape(dst, dst_path):
        return False
    existing = {j.get("name") for j in dst.get("jobs", [])}
    imported = 0
    for job in src.get("jobs", []):
        name = job.get("name")
        if not name or name in existing:
            continue
        job["id"] = hashlib.md5(f"{name}-imported".encode(), usedforsecurity=False).hexdigest()[:8]
        dst.setdefault("jobs", []).append(job)
        imported += 1
    dst_path.write_text(json.dumps(dst, indent=2), encoding="utf-8")
    total = len(src.get("jobs", []))
    print(f"  Cron jobs imported: {imported} (skipped {total - imported} duplicates)")
    return True


_TERMINATORS = (b"\n", b"\r")


# Longest single notification record either side of the merge will materialise.
# Both trees are agent-writable and the read feeds an append to a durable file,
# so an over-cap record aborts rather than being skipped. Named here, and read at
# call time, so a test can move the dial instead of writing a 128 MiB fixture --
# the same reason `subagent_cost` names its own.
_NOTIFICATION_RECORD_CAP = RECORD_CAP


# Largest notification SOURCE this will hold, in bytes. A whole-FILE cap, which the
# streaming predecessor did not need and the single-read design does: the per-record
# cap above bounds one record, not a file made of many.
#
# Sized from the destination's own invariants rather than from a sample.
# ``_MAX_PERSISTED_NOTIFICATIONS`` is 200; ``_maybe_trim_notifications`` runs after
# EVERY append and trims past 200*2; the loader keeps the last 200 and every rewrite
# writes the last 200. So the live file is self-bounding at 400 records at all times,
# and anything beyond 200 is discarded on the next read regardless -- installing more
# than that is transient by construction, which is why refusing a larger source costs
# the operator nothing real. Measured on a live install: 207 records, 370,107 bytes,
# largest single record 20,821 bytes. 400 of that largest record is 8,316,000 bytes,
# so this is ~4x the product-bounded worst case and a quarter of the per-record cap
# the same file already accepts for ONE record.
#
# Peak held is about twice the SOURCE size, not twice this cap: the read accumulates in
# 1 MiB chunks for that reason. A single `read(cap + 1)` preallocates the whole limit,
# which made an 8 MB source cost 32 MiB and tied the peak to the constant rather than to
# the file. Measured on the shipped code: 15.9 MiB for the 8.3 MB worst case below, and
# 1.0 MiB for a realistic 207-record file. Both far under the 128 MiB single allocation
# above.
#
# Over-cap is a REFUSAL naming the size, never a truncation and never a silent skip.
# A cap that dropped the tail would recreate the defect this design removes, one layer
# up: a partial install reported as success.
_NOTIFICATION_SOURCE_CAP = 32 * 1024 * 1024


def _notification_key(record: bytes, path: Path) -> tuple[Any, ...] | None:
    """The dedupe key for one raw notification record, or ``None`` if it has none.

    Well-defined and hashable for EVERY record shape. A naive
    ``json.loads(line).get("ts") or line.strip()`` is not: a non-object record
    raises ``AttributeError`` off ``.get``, and a list or dict ``ts`` raises
    ``TypeError`` on set insert, and both escape as an aborted restore.

    A ``ts`` is used whenever it is truthy AND hashable, which is every JSON
    scalar. Restricting it to ``str`` is WORSE than keying a numeric ``ts`` on
    the number: two rows carrying the same numeric ``ts`` with different bytes
    -- one normalised, one not -- have to deduplicate, and keying them on their
    raw form instead persists a duplicate. Only an unhashable ``ts``, which
    cannot be a set member at all, falls through to the raw form.

    The ``ts`` goes into the key under a KIND TAG that is deliberately coarser
    than its Python type, because two different equalities are in play at once
    and a naive tag gets one of them wrong:

    * ``True == 1`` and ``hash(True) == hash(1)``, so an untagged key makes a row
      with ``ts: true`` and a row with ``ts: 1`` one set member and DELETES the
      second as a duplicate.
    * ``1 == 1.0`` and they hash equal too, so tagging with
      ``type(ts).__name__`` splits a row written as an integer here and a float
      there -- an ordinary serializer artefact -- into two records and PERSISTS a
      duplicate.

    An earlier revision hit each of those in turn. One tag covers both: integers
    and floats share ``"num"`` so they still deduplicate exactly as the
    predecessor's bare-value key did, while ``bool`` is its own tag. ``bool`` is
    tested first because it is a SUBCLASS of ``int``, so an ``isinstance(ts,
    int)`` check would swallow it.

    A record with no usable ``ts`` falls back to its RAW BYTES, and a record that
    does not PARSE gets no key at all. The split is the fix. The predecessor fell
    back to ``line.strip()`` for both, and stripping is what deleted bytes: it
    makes two DISTINCT byte sequences share a key. Unstripped bytes cannot --
    byte-equal records ARE the same record, so collapsing them loses nothing.
    Withholding a key from an unparseable record covers the remaining case,
    because the fragments a split record produces are exactly the unparseable
    ones.

    That is reachable, not theoretical, and it is why the two cases are separated.
    A crash mid-append leaves a truncated row in the live file -- say ``b'{"a":
    "x'``. A source record holding a bare carriage return is split at it, because
    this reader's boundaries are the universal-newline set, yielding ``b'{"a":
    "x\r'``. Those two are NOT byte-equal, but they STRIP to the same thing, so
    the predecessor skipped the fragment as a duplicate, appended only the tail,
    and left the live file with a line parsing as neither while the source's bytes
    were gone. Measured on real bytes, before and after: stripping loses them,
    raw bytes do not. The fragment is also unparseable, so it takes the ``None``
    path and is doubly protected.

    So a ``ts``-less row that parses IS deduplicated, on bytes -- which keeps the
    predecessor's idempotence for a re-run without keeping the deletion. Only an
    unparseable record is appended unconditionally.
    Duplicating a row is recoverable; deleting one is not. ``_merge_notifications``
    validates the whole source before appending anything so that a FAILED merge
    does not leave a prefix for a retry to duplicate.

    The kind tag also keeps the two families apart: ``json.loads`` can only
    produce ``dict``, ``list``, ``str``, ``int``, ``float``, ``bool`` or ``None``,
    so ``type(ts).__name__`` is never ``"raw"`` and a byte key can never collide
    with a ``ts`` key.

    Raises :class:`UndecodableRecord` for a record that is not valid UTF-8,
    which is how the encoding property is enforced: this decode VALIDATES and
    the result is used only for the key, while what gets appended is always the
    original bytes. Validating by decoding and then writing the decoded form
    back is what makes the copy non-byte-exact in the first place.
    """
    try:
        text = record.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UndecodableRecord(f"record is not valid UTF-8 in {path!r}") from exc
    try:
        parsed = json.loads(text)
    except ValueError:
        # A record that does not PARSE keeps no key, so it is never skipped.
        # Deliberately not byte-keyed: framing splits a record at a bare
        # carriage return, and the fragments of a split record are exactly the
        # unparseable ones. Leaving them unkeyed is what makes "a fragment is
        # never mistaken for a record already present" structural rather than a
        # property of whichever collision one happens to think of.
        return None
    ts = parsed.get("ts") if isinstance(parsed, dict) else None
    # Truthiness reproduces the predecessor's `or` fallback: an absent, empty or
    # zero ts fell through to the line itself, and now falls through to None.
    if ts:
        try:
            hash(ts)
        except TypeError:
            pass
        else:
            # `bool` first: it is a subclass of `int`, so the numeric arm would
            # otherwise swallow it and re-create the True/1 collision.
            if isinstance(ts, bool):
                kind = "bool"
            elif isinstance(ts, (int, float)):
                kind = "num"
            else:
                kind = type(ts).__name__
            return (kind, ts)
    # No usable ``ts``, but the record PARSED -- so it is a whole record a
    # producer wrote, not a framing fragment, and its raw bytes are an identity
    # it is safe to deduplicate on. RAW, and never stripped: the predecessor's
    # ``line.strip()`` is what deleted bytes, because stripping makes two
    # DISTINCT byte sequences share a key -- a fragment ending in a carriage
    # return strips to a crash-truncated row that does not contain one. Keying on
    # the unstripped bytes cannot do that: byte-equal records ARE the same
    # record, so a collapse here loses no information, while a stripped key
    # collapses records that differ. That distinction is the whole fix.
    #
    # The key is the bytes that LAND, not the bytes that arrive. The append below
    # terminates an unterminated record, so keying the arriving form makes a
    # re-import compare an unterminated source row against the terminated row it
    # itself wrote, miss, and append a second copy. Normalising here uses the
    # SAME predicate as that write, so the two cannot drift.
    #
    # The direction matters and only one of the two is safe. Normalising by
    # ADDING the terminator the writer adds is deterministic and merges only
    # records that land identically. Normalising by REMOVING terminators would be
    # ``rstrip``, and that is the predecessor's deleter wearing a different name:
    # it maps ``X\r`` and ``X`` onto one key, which is precisely the fragment and
    # crash-truncated-row pair above.
    return ("raw", record if record.endswith(_TERMINATORS) else record + b"\n")


def _merge_notifications(src_path: Path, dst_path: Path) -> None:
    """Append the snapshot's notification records to the live file, byte for byte.

    This is the only merge that COPIES records rather than consuming them, so
    reading faithfully is not the whole contract -- the bytes must also be valid
    for the destination. Both handles are BINARY and framing comes from
    :func:`strict_raw_records`, because the text-mode predecessor was a locale
    decode followed by a locale encode and neither half was byte-exact:

    * Universal-newline translation on read, ``os.linesep`` on write. A record
      terminated ``\\r\\n`` was appended as ``\\n`` -- a byte silently dropped --
      and a bare ``\\r`` INSIDE a record split it in two, so both halves failed
      ``json.loads``, the ``except`` swallowed both, and the record was lost
      while the function printed success. Both fire on a pure UTF-8 host with
      fully valid UTF-8 input, so an explicit ``encoding=`` does not address
      them; ``newline=`` is a separate axis and binary mode closes both at once.
    * The locale codec decided whether an invalid-UTF-8 record aborted the
      restore or was delivered into the live file. ``for line in f`` decodes
      OUTSIDE the ``try``, so ``UnicodeDecodeError`` -- a ``ValueError`` --
      escaped the ``except (ValueError, TypeError)`` because the iterator raised
      it, not ``json.loads``. On a UTF-8 host that aborted the restore with a
      traceback; under a single-byte locale the decode succeeded, the encode put
      the same bytes back, and the live file stopped being valid UTF-8 -- after
      which its loader returns NO rows for the whole file and the next rewrite
      persists that empty view.

    The posture is ABORT, never skip, because the output feeds a durable write
    and a skipped record is a deleted one. Abort means RAISE, not warn: this
    function's callers report an outcome to somebody. ``apply_import_zip``
    appends ``notifications (merged)`` to its summary and the dashboard handler
    answers ``ok: True`` with a SEL ``outcome="ok"``, and a printed warning is
    invisible to both -- so warn-and-return would tell an API caller the import
    succeeded while records were left behind. The print stays so a CLI operator
    reads the reason before the traceback. This is why it differs from
    ``_merge_crons``, which warns and returns: a refused cron merge writes
    nothing and skips one component, while this one may already have appended a
    prefix, and the caller has to learn the write is incomplete.

    * A destination-scan failure is still a true no-op -- the destination is not
      even opened for append until that scan has completed.
    * The SOURCE is validated whole before the destination is opened for append,
      so a source-side refusal is also a no-op. Without that pass, a source whose
      Nth record is undecodable had already appended N-1 records when it aborted,
      and a retry re-appended every identity-less one of them, since a row with
      no ``ts`` cannot be deduplicated. The cost is reading the source twice.
    * A failure DURING the copy is therefore the residual case -- the source
      changed between the two passes -- and its prefix stays. Rolling it back
      would be a second unvalidated write to the live file.

    Every appended record ends with a terminator, and an unterminated final
    record already in the destination gains one before anything is appended
    after it. Without that, two records glued into one line that parses as
    neither.
    """
    facade = _facade()
    existing: set[tuple[Any, ...]] = set()
    dst_unterminated = False
    try:
        with open(dst_path, "rb") as f:
            for record in facade.strict_raw_records(
                f, dst_path, cap=facade._NOTIFICATION_RECORD_CAP
            ):
                key = facade._notification_key(record, dst_path)
                if key is not None:
                    existing.add(key)
                dst_unterminated = not record.endswith(_TERMINATORS)
    except (OSError, UnreadableRecord) as exc:
        # No `_safe_name` here, deliberately: this path is the LIVE data home,
        # chosen by the operator, not a name that came out of an archive -- which
        # is the scope `_safe_name`'s own docstring states. The SOURCE prints do
        # wrap it; see the one below.
        print(f"  ⚠️  Could not read {dst_path}: {exc} — merge aborted")
        raise
    # The ENTIRE source is validated before the destination is opened for append.
    # Without this pass, a source whose Nth record is undecodable or over-cap has
    # already appended N-1 records by the time it aborts -- and a retry
    # re-appends every identity-less one of those, because a row with no ``ts``
    # cannot be deduplicated by construction. Validating first makes the source
    # side all-or-nothing in the ordinary case, so there is no prefix to
    # duplicate.
    try:
        with open(src_path, "rb") as f:
            for record in facade.strict_raw_records(
                f, src_path, cap=facade._NOTIFICATION_RECORD_CAP
            ):
                facade._notification_key(record, src_path)
    except (OSError, UnreadableRecord) as exc:
        # The PATH goes through `_safe_name` because a bundle chooses its own inner
        # root, so an archive-derived path can carry ANSI controls -- and printing
        # one raw lets a hostile archive move the cursor and overwrite lines right
        # above the prompt where the operator decides whether to trust the restore.
        #
        # The EXCEPTION deliberately does NOT, and the invariant is worth stating
        # because it is what makes the wrapper unnecessary rather than forgotten:
        # both types this arm catches already render an embedded path with
        # repr-style escaping -- `OSError.__str__` does it for its filename, and
        # `jsonl_util` uses `{path!r}` for the reason its own comment gives.
        # Measured: a control character in a directory name reaches neither
        # exception's `str()` raw. Widening this `except` tuple means re-checking
        # that, because a type formatting a path with `str()` would need the wrapper.
        print(f"  ⚠️  Could not read {_safe_name(src_path)}: {exc} — merge aborted")
        raise
    imported = 0
    try:
        with open(dst_path, "ab") as out, open(src_path, "rb") as f:
            if dst_unterminated:
                out.write(b"\n")
            for record in facade.strict_raw_records(
                f, src_path, cap=facade._NOTIFICATION_RECORD_CAP
            ):
                key = facade._notification_key(record, src_path)
                # A `None` key means the record did not PARSE, so it may be a
                # framing fragment rather than a record -- nothing it could be a
                # duplicate OF. Both `existing.add` sites refuse `None`, which is
                # what keeps it out of this membership test; an unconditional
                # `key is not None` here would be dead, and a mutation proved it
                # unobservable. A ts-less row that DOES parse is keyed on its raw
                # bytes and deduplicates normally; see _notification_key for why
                # raw and not stripped.
                if key in existing:
                    continue
                out.write(record if record.endswith(_TERMINATORS) else record + b"\n")
                if key is not None:
                    existing.add(key)
                imported += 1
    except (OSError, UnreadableRecord) as exc:
        # Reached only when the source changed BETWEEN the validation pass and
        # this one, so the prefix already appended stays: rolling it back would
        # be a second unvalidated write. Names the count so an operator knows a
        # prefix landed, and identity-less rows in it will re-append on a retry.
        print(f"  ⚠️  Stopped merging {_safe_name(src_path)} after {imported} record(s): {exc}")
        raise
    print(f"  Notifications imported: {imported}")


def _serialise_with_notification_writes(work: Callable[[], None]) -> None:
    """Run *work* in FIFO order with the dashboard's own notification writes.

    ``_install_notifications`` writes the live ``notifications.jsonl`` while the
    gateway may be writing it too, and ``O_APPEND`` alone is not enough. It stops a
    concurrent row being OVERWRITTEN, and then orders it BEFORE the archive's rows:
    the dashboard's append goes to end-of-file immediately while the copy's writes are
    still buffered, so the live row lands first and the archive's follow it. The
    reader's cap is POSITIONAL -- ``_load_notifications`` keeps the last
    ``_MAX_PERSISTED_NOTIFICATIONS`` rows, not the newest by timestamp -- so importing
    a full 200-record history pushes the live row out of the window. Measured: a note
    delivered mid-copy landed at line 0 of 201 and the reader returned 200 rows
    without it. The loss is silent: the operator was told the notification was
    delivered, and after the next reload it is gone.

    Notification persistence runs on ONE worker in submission order
    (``_notification_io_executor``), so running the copy on that same worker is what
    makes the two ordered rather than concurrent. A queued append then runs strictly
    after the copy and lands at the end of the file, where the cap keeps it.

    The trap in deciding whether to serialise is treating the absence of an OBSERVABLE
    writer as the absence of a writer. "No pool" does not mean "no writer"; the writer is
    what makes the pool. A fresh gateway has persisted nothing, so its pool is ``None``,
    and a copy running inline on that basis races a delivery arriving at that moment: the
    delivery CREATES the executor and appends through it, concurrently, putting the live
    row back at line 0 of 201 and outside the reader's window. A broad
    ``except Exception`` on the import one line above is the same error in another form:
    it turns "I could not check" into "there is nothing to check", losing the ordering
    silently inside a live gateway.

    So this ACQUIRES rather than asks. ``_notification_io_executor()`` creates the
    worker if there is none and returns the existing one if there is. That removes the
    "is there a pool" question outright and narrows the import case to ``ImportError``.

    TWO inline cases remain, and neither reads an absence of OBSERVATION as an absence
    of a writer:

    1. Already ON the worker. Then we ARE the ordering point, and submitting would wait
       for a queue only this call can drain -- a deadlock rather than a race.
    2. The dashboard module does not import. A missing MODULE is categorically different
       from a missing pool: the sink lives in that module, so if it is not importable
       there is no writer that could exist, rather than none currently visible. That is
       the CLI restore. Narrowed to ``ImportError`` precisely so it cannot absorb
       anything else -- a module that fails to import for any OTHER reason is a real
       failure and must surface rather than quietly degrade the guarantee.

    Acquiring costs the CLI path one idle worker thread. That is a short-lived process
    and the thread exits at interpreter shutdown; a silent ordering hole is permanent, so
    the trade is not close.

    The executor is released as soon as *work* returns OR raises: ``result()``
    re-raises rather than swallowing, so the failure path needs no separate release
    and cannot leave notification writes blocked.

    Relying on that executor for ordering means its own CREATION has to be ordered too.
    An unlocked check-then-set would let two threads each observe ``None``, each build a
    pool, and never be serialised against one another -- which would void this guarantee
    rather than weaken it. ``dashboard/state.py`` takes a lock around the lazy init, so
    acquiring the executor is itself race-free.
    """
    try:
        from kiro_crew.dashboard import state as dashboard_state
    except ImportError:
        work()
        return
    if threading.current_thread().name.startswith("notif-io"):
        work()
        return
    dashboard_state._notification_io_executor().submit(work).result()


class NotificationCopyUnsupported(Exception):
    """This platform cannot install notification records safely, so it does not.

    Deliberately not an ``OSError``: the copy's own error arm catches ``OSError`` and
    re-raises it as a failed import, which is the wrong shape for "this platform was
    never able to do this". A distinct type lets the callers report a SKIP, and an
    unhandled one still aborts rather than passing silently.
    """


def _install_notifications(src_path: Path, dst_path: Path) -> None:
    """Install the snapshot's notification records where the live file does not exist yet.

    The sibling of ``_merge_notifications``, and the reason it exists separately
    is that the two branches of one ``if`` had different postures: the merge
    validates every source record's encoding and ABORTS on one it cannot deliver
    intact, while this branch was ``shutil.copy2`` and validated nothing. A
    byte-exact copy is correct as a copy and that is exactly the problem -- it
    faithfully installs bytes the destination's own reader refuses.
    ``_load_notifications`` decodes the WHOLE file inside one ``try`` that
    returns ``[]``, so one invalid byte costs every row, and the next
    ``_rewrite_notifications`` -- any delete, ack or clear -- persists that empty
    view. Unlike the merge this fired on every locale, because nothing decoded on
    the way in, and it fired on a fresh install or a first restore, where the
    operator has the least reason to suspect anything.

    So the posture matches the merge: ABORT, never accept, never skip. That is not
    a new product decision, it is the decision the merge branch already carries --
    a snapshot with an undecodable record aborted the restore when a live file
    existed and was installed silently when one did not.

    It is a SEPARATE function rather than a call into the merge with an empty
    destination, and both halves of that were measured, not assumed:

    * ``_merge_notifications`` opens the destination for READ first, so a missing
      one raises ``FileNotFoundError`` out of the arm that guarantees a
      destination-scan failure is a true no-op. Teaching that arm to tell
      "missing, fine" from "unreadable, abort" reopens the fail-closed posture
      that arm exists for.
    * The merge DEDUPLICATES against what it has already written, which a copy
      must not: run four source records -- two sharing a ``ts``, two byte-identical
      without one -- through a merge into an empty destination and two land. There
      is nothing here to deduplicate against, so keying source records against
      each other converts a faithful copy into a lossy one.

    Every path here is resolved to a descriptor EXACTLY ONCE and all later work goes
    through that descriptor. That invariant closes a whole class rather than single
    instances of it: the defect is an operation resolving a name more than once where
    another process can change what the name means, and a fix that only re-checks moves
    which name is vulnerable instead of removing the second resolution. The source is
    opened once
    ``O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_BINARY`` and read once, never seeked; the
    destination is created once
    ``O_CREAT|O_EXCL|O_WRONLY|O_APPEND|O_NOFOLLOW|O_BINARY``. The
    remaining uses of either path -- ``_safe_name`` in the messages, and the ``path``
    argument to ``strict_raw_records`` and ``_notification_key`` -- resolve nothing:
    both callees only interpolate it into an error string.

    That invariant is descriptor-bound on POSIX and CANNOT be on Windows, which has no
    ``O_NOFOLLOW``: there the flag is 0. The predecessor put a by-name reparse-point
    check in front of the open and treated it as a floor; it is not one, because a
    by-name check followed by a by-name open is a window the concurrent agent in this
    threat model chooses the timing of, and ``fstat``'s ``S_ISREG`` does not close it --
    a reparse point resolving to a regular ``.env`` passes it. So this function is not
    reached at all on such a platform: :func:`_copy_notifications` refuses the copy
    before acquiring the executor. Naming the split rather than implying it, because
    the two genuinely differ in what they can promise -- POSIX refuses a link inside
    the open syscall, and a platform without the flag does not attempt the copy.

    The caller reaches this branch on ``sn.is_file()`` and ``not dn.is_file()``, which
    are by-name and therefore advisory. They are not trusted: each is confirmed or
    refuted by the single authoritative resolution here. A destination that filled
    after the check fails ``O_EXCL``, and a source that became a link or a FIFO fails
    ``O_NOFOLLOW`` or the ``S_ISREG`` check on the descriptor. What is NOT closed here
    is the ancestor chain -- both opens name a directory rather than a pinned
    descriptor -- and that is deliberate: every core-file copy in this function
    reaches its path the same way, so pinning one of ten sites would be the point
    patch review already named. It is an axis for its own change.

    ONE read of the source, and the ordering is the whole design:

    1. The source is read ONCE, whole, under a byte cap, and the file is never
       touched again. Everything after that reads the bytes in hand.
    2. Those bytes are validated in full. A refusal here happens with the
       destination never created, so there is nothing to roll back -- which matters
       because there is no safe rollback: creating the live file and unlinking it on
       refusal loses data -- ``apply_import_zip`` runs inside the live gateway, so the
       dashboard's notification sink can append to that file first and the unlink
       takes the operator's notification with it.
    3. The destination is then created
       ``O_CREAT|O_EXCL|O_WRONLY|O_APPEND|O_NOFOLLOW|O_BINARY`` and the validated
       bytes are written. ``O_EXCL`` decides inside one syscall, so a name that
       filled after the caller's ``is_file()`` check is refused rather than written
       through -- a dangling symlink at that name included, which ``is_file()``
       reports as absent and ``copy2`` followed, writing the archive's bytes outside
       the data home.

    Reading once is not an optimisation, it is what makes a whole class of defect
    UNREPRESENTABLE rather than detected. Reading the file twice -- validate, then
    install -- leaves a window with many different ways for the source to change
    inside it: swapped for a symlink to a credential, reopened by name, truncated so
    the second pass meets a clean EOF and reports success having installed fewer
    records than it validated. Closing one variant only exposes the next, because a
    check can only catch the case someone thought of. With the bytes held in memory
    there is no name
    left to resolve and no handle left open, so there is nothing for another process
    to swap, truncate or extend. The two loops below are two passes over the same
    immutable bytes, which is safe for exactly the reason two passes over the FILE
    were not.

    That trade needs a number, not a preference, and the number is the destination's
    own bound: see ``_NOTIFICATION_SOURCE_CAP``. Peak held is about twice the SOURCE
    size rather than twice the cap -- measured at 15.9 MiB for the 8.3 MB
    product-bounded worst case and 1.0 MiB for a realistic 207-record file, against
    the 128 MiB this same file already accepts for a SINGLE record. A source over the
    cap is refused with its size named -- never truncated, never partially imported,
    because a silently dropped tail is the defect being removed wearing a hat.

    No temporary file, deliberately: a temp file in the data home is published through
    a NAME, and a same-user process that can list that directory can swap what the name
    holds between the write and the
    publish -- which links bytes this function never validated into place as
    ``notifications.jsonl``. The mitigations for that are inode verification on both
    ends plus a non-hardlink fallback (``pinned_fs.put_back_no_clobber`` is the
    repo's audited version, and its own docstring notes the landed check narrows the
    window rather than closing it). Writing straight to an ``O_EXCL`` destination
    needs none of it: there is no intermediate name to swap.

    What this does NOT close: everything on the DESTINATION side. ``O_EXCL`` refuses a
    name that filled, ``O_APPEND`` keeps a concurrent notification from being
    overwritten, and both remain necessary -- the live file has other writers and
    reading the source once says nothing about them. Only the read window is gone.

    Records are written verbatim -- what is validated is the source's bytes, never a
    decoded form of them -- with a single repair: an unterminated final record gains
    a terminator. That is not cosmetic once the write is record-wise.
    ``_persist_notification`` appends ``json.dumps(note) + "\\n"``, so the first
    notification after the restore would otherwise glue onto an unterminated last
    line and produce one line that parses as neither row. It is the same repair the
    merge makes through ``dst_unterminated``.
    """
    facade = _facade()
    # `_notification_key`'s result is discarded -- it is called for the
    # `UndecodableRecord` it raises, which its own docstring documents as how the
    # encoding property is enforced. Reusing the merge's predicate rather than
    # inlining a second decode is what keeps the two branches' acceptance criteria
    # identical, and a second decode is exactly how they drifted apart in the first
    # place.
    #
    # The SOURCE is resolved exactly once and read exactly once. The revision before
    # this one opened the name once per pass, and between the two a running agent
    # could replace the extracted file with a symlink to `.env`: the second open
    # followed it and the secret landed in an agent-readable `notifications.jsonl`.
    # `O_NOFOLLOW` refuses a link at the name instead of following it -- consistent
    # with `_backup_and_copy`, which already skips a symlinked file coming out of an
    # archive -- and reading once removes the second resolution the flag was
    # protecting.
    #
    # `O_NONBLOCK` closes the other way that by-name selection misleads this open,
    # which review did not name: the caller chose this branch on `is_file()`, and a
    # name that became a FIFO afterwards would block the open forever and hang the
    # restore rather than failing it. The kind is then judged on the DESCRIPTOR with
    # `fstat`, NOT by re-checking the name -- a second by-name check would be the
    # same mistake one layer down, whereas a held descriptor cannot be swapped.
    src_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )
    # 0o666 so the kernel applies the umask, giving the same mode as the `open(path,
    # "a")` in `_persist_notification`: a restored file must not be tighter than one
    # the product wrote itself.
    #
    # `O_APPEND` because the dashboard's notification sink writes to this same file
    # with it, and its append goes to end-of-file while an ordinary write goes to
    # THIS handle's offset. Buffered, that offset is stale by the time it flushes, so
    # a notification delivered mid-copy would be overwritten by the flush. With
    # `O_APPEND` every write lands at end-of-file, so the two writers
    # interleave instead of clobbering. On the fresh file `O_EXCL` guarantees, it
    # changes nothing about the ordinary outcome.
    #
    # `O_BINARY` on BOTH, because `os.open` is the one API here that can be in text
    # mode: the repo documents it as required on Windows and every sibling passes it,
    # `crash_dump_store.py` reaching for this exact read-flag triple. A no-op on
    # POSIX, where the constant does not exist. It is a CONVENTION fix and not a
    # corruption fix -- the corruption a review lane described is refuted by
    # `test_a_clean_source_is_copied_record_for_record`, which asserts byte-exact
    # `\\r\\n` survival through these very descriptors and passes on the Windows lane.
    dst_flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    written = 0
    opened_dst = False
    try:
        # No platform floor is attempted here. A platform with no `O_NOFOLLOW` to give
        # never reaches this function: `_copy_notifications` refuses the whole copy up
        # front, because a by-name `is_reparse_point` check followed by a by-name open
        # is a check-to-open window and `fstat`'s `S_ISREG` does not close it -- a
        # reparse point resolving to a regular `.env` passes it. So this open always
        # carries a real `O_NOFOLLOW` and the refusal is decided by the open itself.
        # An `lstat`-then-`fstat` identity comparison is deliberately NOT done -- that
        # one pretends to close a window it merely narrows.
        with os.fdopen(os.open(src_path, src_flags), "rb") as src:
            if not _stat.S_ISREG(os.fstat(src.fileno()).st_mode):
                raise OSError("source is not a regular file")
            # ONE read of the file, and the file is not touched again. Chunked, and
            # that is not incidental: `read(cap + 1)` in one call PREALLOCATES a
            # buffer the size of the cap, so an 8 MB source cost 32 MiB and the peak
            # was a function of the limit rather than of the file. Measured, which is
            # the only reason it was noticed. Accumulating 1 MiB at a time keeps the
            # peak proportional to the actual source.
            #
            # The cap is enforced on bytes ALREADY READ, not on a separately-stated
            # size: `st_size` can be stale by the time it is compared, and the bytes
            # in hand cannot be. Enforced inside the loop, so an oversized source is
            # abandoned as soon as it crosses the line instead of being materialised
            # first.
            acc = bytearray()
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                if len(acc) + len(chunk) > facade._NOTIFICATION_SOURCE_CAP:
                    raise OSError(
                        f"notification source is at least "
                        f"{len(acc) + len(chunk)} bytes, over the "
                        f"{facade._NOTIFICATION_SOURCE_CAP} byte limit -- refusing rather "
                        "than importing part of it"
                    )
                acc.extend(chunk)
            blob = bytes(acc)
        # Everything below reads MEMORY. The two loops are two passes over the same
        # immutable bytes, which is what makes them safe where two passes over the FILE
        # were not: nothing between them can swap the source for a symlink, truncate it,
        # or append to it, because there is no name left to resolve and no file handle
        # left open. The window is not detected here, it is unrepresentable.
        #
        # Framing goes through `strict_raw_records` over a `BytesIO` rather than a
        # hand-rolled split, so the record boundaries are the SAME implementation the
        # merge branch uses. A second splitter is how two paths drift apart, which is
        # the defect this whole change exists to fix.
        with io.BytesIO(blob) as buf:
            for record in facade.strict_raw_records(
                buf, src_path, cap=facade._NOTIFICATION_RECORD_CAP
            ):
                facade._notification_key(record, src_path)
        with os.fdopen(os.open(dst_path, dst_flags, 0o666), "wb") as out:
            opened_dst = True
            with io.BytesIO(blob) as buf:
                for record in facade.strict_raw_records(
                    buf, src_path, cap=facade._NOTIFICATION_RECORD_CAP
                ):
                    out.write(record if record.endswith(_TERMINATORS) else record + b"\n")
                    written += 1
    except (OSError, UnreadableRecord) as exc:
        # Two outcomes, told apart by whether the destination was ever created:
        # nothing written at all (a bad archive, a refused source, or a name that
        # filled after the caller's check), or a prefix that STAYS. Every record in a
        # prefix passed validation, and unlinking is what took a concurrent writer's
        # file in the revision review blocked, so the count is named instead.
        #
        # `_safe_name` on the PATH because a bundle chooses its own inner root, so an
        # archive-derived path can carry ANSI controls and printing one raw lets a
        # hostile archive overwrite the lines right above the operator's prompt. The
        # EXCEPTION does not need it, for the reason `_merge_notifications` states:
        # both types this arm catches already render an embedded path with repr-style
        # escaping.
        tail = f"{written} imported" if opened_dst else "notifications not imported"
        print(f"  ⚠️  Could not copy {_safe_name(src_path)}: {exc} — {tail}")
        raise


def _copy_locked(src: Path, dst: Path) -> bool:
    """Copy *src* onto a missing *dst*, owner-only before the name is published.

    Merge restore only copies when *dst* is absent. Publish with ``os.link``
    (the create-only shape ``_get_telemetry_salt`` uses) so a dest that
    appears in the window — ``--force`` restore racing a live gateway
    creating ``telemetry_salt`` — raises ``FileExistsError`` and the live
    file is left alone. ``os.replace`` / ``atomic_write`` would clobber it.
    The temp is locked down before any payload is written. Failures to
    publish (unsupported hard links, a restrict error, a size mismatch)
    skip this file without raising, because merge restore has already
    applied earlier components and ``_get_telemetry_salt`` regenerates a
    missing salt. Return True only when this call published *dst*.
    """
    if dst.exists():
        return False
    if src.name == "telemetry_salt" or dst.name == "telemetry_salt":
        size = src.stat().st_size
        if size != _TELEMETRY_SALT_BYTES:
            return False
    payload = src.read_bytes()
    if dst.exists():
        return False
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=str(dst.parent))
    tmp_path = Path(tmp)
    try:
        platform_compat.restrict_to_owner(str(tmp_path))
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written == 0:
                raise OSError(f"short write restoring {src.name}")
            view = view[written:]
        # Drop the fd from finally before close: a close-time writeback
        # error must not be retried on an already-released descriptor,
        # because a second OSError in finally would replace the skip and
        # abort merge after earlier components were applied.
        pending = fd
        fd = -1
        os.close(pending)
        os.link(str(tmp_path), str(dst))
        return True
    except OSError:
        # FileExistsError: live dest won. EXDEV / EPERM / no-hardlink /
        # restrict / short write / close: dest stays missing and
        # `_get_telemetry_salt` regenerates. Raising here aborts merge
        # after earlier components were already applied.
        return False
    finally:
        if fd >= 0:
            pending = fd
            fd = -1
            try:
                os.close(pending)
            except OSError:
                pass
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _merge_named_stores(
    src_root: Path, dst_root: Path, *, allow_unpinned: bool = False
) -> list[str]:
    """Install absent stores whole and return the names kept whole at the destination.

    A manifest and its databases belong to one generation: filling missing files in an
    existing store can combine a V2 manifest with a V1 database that cannot open as V2.
    """
    facade = _facade()
    with memory_store_namespace_lock(dst_root):
        platform_compat.make_owner_only_dir(dst_root)
        kept: list[str] = []
        for src in sorted(src_root.iterdir()):
            if is_host_local_store_state((MEMORY_STORES_DIR_NAME, src.name)) or not src.is_dir():
                continue
            dst = dst_root / src.name
            if dst.exists():
                kept.append(src.name)
                continue
            facade._copytree_safe(
                src,
                dst,
                allow_unpinned=allow_unpinned,
                must_create=True,
                on_skip=pinned_fs.fatal_skip_reporter(f"merge of store {src.name!r}"),
            )
            platform_compat.make_owner_only_dir(dst)
        return kept


def _report_unmerged_databases(src_tree: Path, dst_tree: Path, tree: str) -> None:
    """Say when merge is about to KEEP a product database rather than merge it.

    Merge copies trees without overwriting, which is right for markdown: a local file that
    is newer than the bundle's must survive. Applied to one of our own databases it means
    the incoming rows are silently dropped — the operator asked to merge their knowledge
    library and got a success message that imported none of it.

    Merging those rows for real is not a copy. `knowledge.db` carries an FTS5 index plus
    foreign keys spanning `sources`, `items`, `mentions` and `source_locations`, so a
    correct merge has to remap keys, rebuild the derived index, and first decide what makes
    two documents the same document. `_merge_memory` is a hand-built per-table merge for
    exactly that reason, and there is no equivalent here yet.

    Until there is, the honest thing is to name it. Silence is what turns a known
    limitation into apparent data loss.

    Named stores are handled separately by `_merge_named_stores`, which keeps an
    existing store whole rather than copying missing files into its generation.
    """
    prefix = f"{tree}/"
    for src in sorted(src_tree.rglob("*")):
        if not src.is_file():
            continue
        leaf = src.relative_to(src_tree).as_posix()
        rel = prefix + leaf
        if not is_product_tree_database(rel) or not (dst_tree / leaf).is_file():
            continue
        print(
            f"  ⚠️  {_safe_name(rel)}: kept the existing database; the bundle's copy was NOT "
            "merged into it.\n"
            "      Merge mode does not combine this database's rows. To take the "
            "bundle's copy instead, use --mode replace."
        )
