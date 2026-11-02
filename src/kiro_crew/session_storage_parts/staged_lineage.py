"""What the trash holds that a chat-image reap must know: staged stems and their lineage.

An owner module of :mod:`kiro_crew.session_storage`, the one import path: callers,
tests and documents reach every name here as ``kiro_crew.session_storage.<name>``.

A transcript's chat-image copies live in the artifact store, keyed by the session,
not in the trash batch that holds the transcript. So the permanent-delete paths
that reap those copies need two facts from the trash: which transcripts an empty
is about to destroy (:func:`staged_transcript_stems`, read before the manifest
goes with the batch, then :func:`batch_is_gone` per batch afterwards), and the fork
edges of every staged transcript (:func:`staged_transcript_lineage`), because a
trashed fork is restorable and must keep protecting its source's copies. Every
reader here streams the agent-writable manifests and bounds what it retains.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

from kiro_crew import hooks, platform_compat
from kiro_crew.config.paths import data_home
from kiro_crew.dashboard.fork_lineage import (
    MAX_METADATA_LINE_BYTES,
    UNPROVABLE,
    parent_key,
    recorded_chain,
)
from kiro_crew.history import MAX_TRANSCRIPT_DIRECTORY_ENTRIES, MAX_TRANSCRIPT_STEM_CHARS
from kiro_crew.session_storage_parts.batch_manifest import (
    _stream_manifest_entries,
    _UnreadableManifest,
)
from kiro_crew.session_storage_parts.file_moves import _batch_dir, _mutation_lock, _staged_path
from kiro_crew.session_storage_parts.store_layout import (
    _TRANSCRIPT_SUFFIX,
    STAGE_CREW_LEAF,
    SessionStorageError,
    trash_root,
)

#: Longest metadata line the staged-lineage reader will parse: the bound shared
#: with the live-catalog reader (``fork_lineage.MAX_METADATA_LINE_BYTES``). A
#: real metadata line is a few hundred bytes; the trash is agent-writable, so
#: the read is capped rather than trusting the file.
_META_LINE_CAP = MAX_METADATA_LINE_BYTES
#: Most staged transcripts whose stems or lineage are retained in one read: the
#: transcript directory's own entry bound, so the trash and the live directory
#: are one population under one policy. Each retained entry is bounded (a key up
#: to ``MAX_TRANSCRIPT_STEM_CHARS``, a chain up to ``MAX_FORK_ANCESTORS`` entries),
#: so this caps the whole structure; a trash past it is unprovable and the reap
#: keeps everything.
MAX_STAGED_LINEAGE_TRANSCRIPTS = MAX_TRANSCRIPT_DIRECTORY_ENTRIES
#: Longest name ``_UNIT_ID_RE`` admits (its ``{0,199}`` tail plus the first
#: character); checked on a raw directory name before the pattern is consulted.
_UNIT_ID_MAX_CHARS = 200


def _staged_transcript_stem(rel: Any) -> str | None:
    """The transcript stem a manifest ``rel`` stages, or ``None`` when it stages
    something else. Only ``crew/<stem>.jsonl`` counts: a rotated archive segment
    or an attachments directory names the same session, and the replay log under
    ``cli/`` is kiro-cli's, whose id is not a transcript stem."""
    if not isinstance(rel, str):
        return None
    parts = PurePosixPath(rel).parts
    if len(parts) == 2 and parts[0] == STAGE_CREW_LEAF and parts[1].endswith(_TRANSCRIPT_SUFFIX):
        return parts[1][: -len(_TRANSCRIPT_SUFFIX)]
    return None


def _retain_stem(stem: str, batch_name: str, retained: int) -> int:
    """Count one retained stem against the shared bounds; raises past either."""
    if len(stem) > MAX_TRANSCRIPT_STEM_CHARS:
        raise SessionStorageError(
            f"trash batch {batch_name!r} stages a transcript name over "
            f"{MAX_TRANSCRIPT_STEM_CHARS} chars"
        )
    retained += 1
    if retained > MAX_STAGED_LINEAGE_TRANSCRIPTS:
        # The trash is agent-writable: a manifest can name any number of
        # transcripts, so what is retained is bounded like every other lineage
        # structure, and the caller fails the reap closed past it.
        raise SessionStorageError(
            f"trash stages more than {MAX_STAGED_LINEAGE_TRANSCRIPTS} "
            "transcripts; lineage is unprovable"
        )
    return retained


def _entry_rels(entry: Any) -> list[Any]:
    files = entry.get("files") if isinstance(entry, dict) else None
    return (
        [r.get("rel") if isinstance(r, dict) else None for r in files]
        if isinstance(files, list)
        else []
    )


def staged_transcript_stems(batch_ids: list[str]) -> dict[str, set[str]]:
    """The transcript stems each staged batch holds: ``{batch_id: {stem, ...}}``.

    Emptying a batch destroys its transcripts for good, and a transcript's chat
    images live OUTSIDE the batch, so the caller that empties must learn which
    sessions are going BEFORE the empty, because the manifest goes with the
    batch. Read under the mutation lock so a batch mid-staging is either
    complete or absent, like ``staged_targets``. A batch that cannot be read
    contributes an empty set: nothing is guessed about it.
    """
    out: dict[str, set[str]] = {}
    retained = 0
    with _mutation_lock():
        for batch_id in batch_ids:
            # Every batch is a retained entry too, even an empty or unreadable
            # one, so the shared cap bounds the batch count as well as the stems.
            if batch_id not in out and len(out) >= MAX_STAGED_LINEAGE_TRANSCRIPTS:
                raise SessionStorageError(
                    f"more than {MAX_STAGED_LINEAGE_TRANSCRIPTS} trash batches named; "
                    "lineage is unprovable"
                )
            stems: set[str] = set()
            try:
                batch_dir = _batch_dir(batch_id)
            except SessionStorageError:
                out[batch_id] = stems
                continue
            try:
                # Streamed: the manifest is O(sessions) and agent-writable, so the
                # entries are never held as a list; a listing that cannot be read
                # to the end is discarded whole rather than kept in part.
                for entry in _stream_manifest_entries(batch_dir):
                    for rel in _entry_rels(entry):
                        stem = _staged_transcript_stem(rel)
                        if stem is not None:
                            retained = _retain_stem(stem, batch_id, retained)
                            stems.add(stem)
            except _UnreadableManifest:
                stems = set()
            out[batch_id] = stems
    return out


def batch_is_gone(batch_id: str) -> bool:
    """``True`` only when the batch directory is POSITIVELY absent.

    ``list_trash`` cannot answer this: it OMITS a batch it cannot offer for
    restore and answers ``[]`` when the root cannot be read, which would make a
    RETAINED batch look gone. Absence is proved by ``lstat`` raising
    ``FileNotFoundError``; a directory that exists, a link, an invalid id, or
    any other error reads as remaining.
    """
    try:
        path = _batch_dir(batch_id)
    except SessionStorageError:
        return False
    try:
        os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def staged_transcript_lineage() -> dict[str, dict[str, Any] | None]:
    """Fork lineage of EVERY staged transcript: ``{stem: meta | None}``.

    ``meta`` is the transcript's metadata line restricted to the lineage keys
    (``forked_from``, ``fork_ancestors``); a transcript whose first line is a
    message reads as ``{}`` (readable, no lineage). ``None`` means the staged
    file or its first line could not be read, which a caller treats as
    unreadable (fail closed). A staged transcript is restorable, so the reap
    counts it as a survivor AND follows its edges: a trashed fork keeps
    protecting its live source's copies, or a restore renders broken images.

    Unlike ``list_trash``, which OMITS a batch it cannot offer for restore, this
    raises :class:`SessionStorageError` for any batch it cannot inspect: a
    silently skipped batch could hold the very transcript the reap must keep,
    so the caller fails the whole reap closed on the raise.
    """
    out: dict[str, dict[str, Any] | None] = {}
    retained = 0
    with _mutation_lock():
        root = trash_root()
        try:
            if platform_compat.is_link_or_junction(root):
                raise SessionStorageError("trash root is a link")
            if data_home().resolve() not in root.resolve().parents:
                raise SessionStorageError("trash root does not live under the data home")
            # Lazily, and bounded BEFORE any entry is retained: the root is
            # agent-writable, so the listing is never materialized whole.
            entries = 0
            for candidate in root.iterdir():
                entries += 1
                if entries > MAX_STAGED_LINEAGE_TRANSCRIPTS:
                    raise SessionStorageError(
                        f"trash root holds more than {MAX_STAGED_LINEAGE_TRANSCRIPTS} "
                        "entries; lineage is unprovable"
                    )
                if len(candidate.name) > _UNIT_ID_MAX_CHARS:
                    raise SessionStorageError("trash root holds an entry with an over-long name")
                retained = _retain_staged_lineage(candidate, out, retained)
        except FileNotFoundError:
            # No trash yet: nothing is staged, and that is provable.
            return out
        except OSError as exc:
            raise SessionStorageError("trash root cannot be listed") from exc
    return out


def _retain_staged_lineage(
    candidate: Path, out: dict[str, dict[str, Any] | None], retained: int
) -> int:
    """Fold one trash-root entry's staged transcripts into ``out`` (see
    :func:`staged_transcript_lineage`); returns the running retained count."""
    if platform_compat.is_link_or_junction(candidate):
        raise SessionStorageError(f"trash batch {candidate.name!r} is a link")
    if not candidate.is_dir():
        return retained
    try:
        batch_dir = _batch_dir(candidate.name)
    except SessionStorageError as exc:
        raise SessionStorageError(f"trash batch {candidate.name!r} cannot be inspected") from exc
    try:
        for entry in _stream_manifest_entries(batch_dir):
            for rel in _entry_rels(entry):
                stem = _staged_transcript_stem(rel)
                if stem is None:
                    continue
                retained = _retain_stem(stem, batch_dir.name, retained)
                staged = _staged_path(batch_dir, rel)
                meta = _read_lineage_meta(staged, batch_dir) if staged is not None else None
                if stem in out and out[stem] != meta:
                    # The same stem staged twice with different lineage: neither
                    # record can be preferred without a rule the reap could be
                    # steered by, so the stem reads as unreadable and the caller
                    # keeps every copy. `None` is sticky: a conflict is never
                    # resolved by a third, agreeing record.
                    meta = None
                out[stem] = meta
    except _UnreadableManifest as exc:
        raise SessionStorageError(
            f"trash batch {candidate.name!r} has no readable manifest"
        ) from exc
    return retained


def _read_lineage_meta(path: Path, batch_dir: Path) -> dict[str, Any] | None:
    """The lineage keys of the metadata line at ``path``, a transcript staged
    under ``batch_dir``. Read through the sensitive-path chokepoint, pinned to
    the batch root and to ``_META_LINE_CAP`` bytes: a staged name swapped for a
    link or a hardlink to a file outside the batch is refused by the read
    itself, not by a check made before it."""
    head = hooks.safe_read_file_bytes_nolink(
        str(path),
        within_root=str(batch_dir),
        max_bytes=_META_LINE_CAP + 1,
        allow_truncate=True,
    )
    if head is None:
        return None
    first, _newline, _rest = head.partition(b"\n")
    if len(first) > _META_LINE_CAP:
        return None
    try:
        data = json.loads(first.decode("utf-8").strip() or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data and data.get("_type") != "metadata":
        # A transcript whose first line is a message: no metadata, readable.
        return {}
    # Validated and bounded HERE, at the retention point, by the readers every
    # other lineage consumer uses: an unprovable parent or chain makes the record
    # unreadable (``None``) and nothing oversized is kept.
    parent = parent_key(data)
    chain = recorded_chain(data)
    if parent is UNPROVABLE or chain is None:
        return None
    out: dict[str, Any] = {}
    if isinstance(parent, str):
        out["forked_from"] = parent
    if chain:
        out["fork_ancestors"] = chain
    return out
