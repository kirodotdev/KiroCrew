"""What one backup cycle is handed and what it reports.

The plan a cycle starts from (:class:`BackupSet`) and the result it ends with
(:class:`CycleResult`), plus the archive walk's own refusal record (:class:`_ArchiveWalkSink`)
and :func:`_name_unreached_remainder`, which names what a stopped phase did not reach. The
archive puts one entry per segment through the result and the sink every cycle, and
retention-off lets it grow without bound, so both keep a capped, length-bounded sample of names
next to an exact count: the counts carry every decision and the samples carry the names an
operator reads. The sample caps live here with them.

Part of :mod:`container.sidecar.backup`, which re-exports every name here and is the import
path callers use. New fields of the plan or the result, and new bounded samples, go here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from .backup_durable import Snapshot

#: How many unreached objects a stop or deadline gate names BY NAME before it stops looking
#: and records one count-free remainder marker instead. The gate is reached mid-stream over a
#: LAZY iterator whose tail is the archive tree, and retention-off lets that tree grow without
#: bound -- so draining the iterator to name every unreached object rebuilds the whole
#: inventory in a list at exactly the moment the cycle is trying to stop, the same
#: unbounded-retention hazard the streamed enumeration removes from the walk. The cycle's
#: completeness reads only whether ANYTHING was refused (see :attr:`CycleResult.complete`), so
#: the current item alone already makes it incomplete; the bounded sample gives an operator
#: names to act on without walking the tail. Sized to match the archive sample cap so a small
#: remainder is named whole and the tests that assert exact skipped-name sets are unaffected.
_UNREACHED_SAMPLE_CAP = 64

#: The count-free marker recorded once when the unreached remainder runs past the sample cap.
#: It is a refusal like any other -- it withholds the pair and makes the cycle incomplete --
#: but names no object, because naming the rest would mean draining the tail this marker
#: exists to avoid.
_UNREACHED_REMAINDER_NAME = "<remainder>"


#: How many entries the bounded per-object records keep BY NAME before keeping only the
#: running count. The ONE cap for every structure that samples the same object population:
#: :class:`CycleResult`'s uploaded/unchanged/gone/refused/unreachable records AND
#: :class:`_ArchiveWalkSink`'s archive-walk refusals, which fold into ``CycleResult.refused``.
#: Both are capped here so one refusal population is not truncated by two different numbers.
#: The archive puts one entry per segment through these every cycle, so an unbounded list
#: would hold the whole inventory for the log's sake alone. Sized well above any real cycle's
#: working set, so an ordinary run's lists are complete and the tests that assert exact
#: contents are unaffected.
_RESULT_SAMPLE_CAP = 4096

#: How many characters of a refusal's REASON are kept in the sample. A refusal carries an
#: exception string, and a store or transport error can be arbitrarily long (a stack-shaped
#: message, an echoed payload), so a capped COUNT of entries is not enough on its own -- one
#: entry can still be huge. The reason is truncated to this, with a marker, so a sampled
#: refusal costs a bounded number of bytes.
_REFUSAL_REASON_MAX_CHARS = 200


#: How many characters of a sampled IDENTIFIER -- an object key or a file name -- are kept.
#: The identifiers these samples retain are walk-built archive paths, and an agent is free to
#: write a transcript under an arbitrarily deep, arbitrarily long archive path; a cap on the
#: COUNT of sampled entries bounds how MANY are kept but not the length of each, so a
#: pathological tree could still exhaust the sidecar's memory through one deeply-nested path
#: string per entry. Every retention point that keeps a name or key truncates it to this with
#: a marker, the same way :func:`_bounded_identifier` and the reason cap bound the other
#: field -- so a sampled entry costs a bounded number of bytes in BOTH its fields, and
#: retained identifier length cannot grow the sidecar's memory without bound. The counts the
#: completeness and withhold decisions read are untouched; only the sampled strings are cut.
_IDENTIFIER_MAX_CHARS = 256


def _bounded_identifier(identifier: str) -> str:
    """Truncate a sampled object key or file name to :data:`_IDENTIFIER_MAX_CHARS`.

    The sample caps limit how many identifiers are retained; this limits how long each one
    is, because a walk-built archive path has no bound of its own. A marker makes a cut one
    visible in the log. Applied at every retention point that appends a name or key to a
    bounded sample, so no sampled field is left unbounded.
    """
    if len(identifier) <= _IDENTIFIER_MAX_CHARS:
        return identifier
    return identifier[:_IDENTIFIER_MAX_CHARS] + "… (truncated)"


def _bounded_reason(reason: str) -> str:
    """Truncate a sampled refusal/unreachable reason to :data:`_REFUSAL_REASON_MAX_CHARS`.

    A reason is an interpolated ``OSError`` string that carries a near-PATH_MAX filename, so
    like the identifier it has no bound of its own. The sample caps limit how many reasons
    are retained; this limits how long each one is. Applied at every retention point that
    appends a reason to a bounded sample -- the sink below as well as :class:`CycleResult` --
    so a reason is never the one field a bounded sample keeps whole.
    """
    if len(reason) <= _REFUSAL_REASON_MAX_CHARS:
        return reason
    return reason[:_REFUSAL_REASON_MAX_CHARS] + "… (truncated)"


class _ArchiveWalkSink:
    """The bounded record of what the archive walk could not enumerate.

    :func:`_archived_segments` streams file paths so the whole inventory is never held in a
    list at any level. Its REFUSALS -- a directory the walk met an error on, a linked
    subdirectory it dropped -- are the other per-archive collection, and are bounded here to
    a capped sample plus a true count rather than an unbounded list. ``refused`` withholds
    the authority pair; ``unreachable`` does not, split on the same permanence rule
    :class:`RefusedEntry` states.

    The counts are what the cycle's completeness and withhold decisions read (a refusal is
    present or it is not); the sampled names are for the log and the incompleteness report.
    """

    __slots__ = ("refused", "unreachable", "refused_count", "unreachable_count")

    def __init__(self) -> None:
        self.refused: list[tuple[str, str]] = []
        self.unreachable: list[tuple[str, str]] = []
        self.refused_count = 0
        self.unreachable_count = 0

    def reset(self) -> None:
        """Empty the record for a fresh walk.

        ``BackupSet.data`` is re-iterable and each iteration re-walks the archive, so the
        sink is reset at the start of each walk -- it then reflects the most recent walk,
        which in the one place that reads it (:func:`run_cycle`, after its single upload-phase
        consumption) is the only walk.
        """
        self.refused = []
        self.unreachable = []
        self.refused_count = 0
        self.unreachable_count = 0

    def refuse(self, entry: tuple[str, str]) -> None:
        self.refused_count += 1
        if len(self.refused) < _RESULT_SAMPLE_CAP:
            name, reason = entry
            self.refused.append((_bounded_identifier(name), _bounded_reason(reason)))

    def cannot_reach(self, entry: tuple[str, str]) -> None:
        self.unreachable_count += 1
        if len(self.unreachable) < _RESULT_SAMPLE_CAP:
            name, reason = entry
            self.unreachable.append((_bounded_identifier(name), _bounded_reason(reason)))


@dataclass
class CycleResult:
    """What one cycle did, per object, for the log and for the tests."""

    #: The keys uploaded / found unchanged this cycle. Bounded: a capped SAMPLE of the keys
    #: plus a true count (``uploaded_count`` / ``unchanged_count``), because the archive
    #: contributes one entry per segment every cycle and retention-off lets the archive grow
    #: without bound -- an unbounded list here would hold the whole inventory the streamed
    #: enumeration exists to avoid holding. Nothing reads their IDENTITY to decide anything
    #: (the summary reads the counts; the withhold and completeness checks read ``refused`` /
    #: ``unreachable`` / ``gone_referenced``), so a sample serves the log while the count
    #: stays exact. Recorded through :meth:`record_uploaded` / :meth:`record_unchanged`.
    uploaded: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    uploaded_count: int = 0
    unchanged_count: int = 0
    gone: list[str] = field(default_factory=list)
    above_ceiling: list[str] = field(default_factory=list)
    #: Exact counts for ``gone`` / ``above_ceiling`` above and ``gone_undurable`` /
    #: ``gone_referenced`` below. Same reason as the other lists: the archive puts one entry
    #: per segment through each of these, so they are capped, count-tracked samples. Nothing
    #: reads their identity to decide anything -- the withhold decision reads
    #: ``gone_referenced_count`` and the summary reads the counts.
    gone_count: int = 0
    above_ceiling_count: int = 0
    refused: list[tuple[str, str]] = field(default_factory=list)
    #: Entries whose BYTES could not be reached at all: a name that is not a regular file,
    #: an archive root that is a link, a subtree that could not be enumerated. Held apart
    #: from ``refused`` because the two decide the authority pair differently, and only
    #: their CAUSE says which is which.
    #:
    #: A refusal means an object the pair can name is not in the bucket, so publishing the
    #: pair would point the front at bytes that are not there -- the pair is withheld. An
    #: unreachable entry is not an object the pair names: the backend never wrote it, so
    #: the index does not reference it and withholding the pair protects nothing. It only
    #: freezes it, and it freezes it FOREVER, because a planted name stays planted: every
    #: later cycle meets the same entry, the pair is never republished, and a replacement
    #: task restores an index from before the entry appeared while transcripts keep
    #: uploading past it.
    #:
    #: The cycle is incomplete either way, which is why this is a second list and not a
    #: log line: something in the set did not reach the bucket and the exit code has to
    #: say so.
    unreachable: list[tuple[str, str]] = field(default_factory=list)
    #: Exact counts for the two lists above. Like ``uploaded``/``unchanged``, the lists are a
    #: capped, length-bounded SAMPLE while these stay exact: on a pathological run every
    #: archived segment can fail its upload, so an entry per failure -- each carrying a full
    #: exception string -- would retain the whole inventory on the failure path, the same
    #: unbounded-retention hazard the streamed enumeration removes from the success path. The
    #: withhold and completeness checks read only whether these are NON-EMPTY (see
    #: :attr:`complete`), which the sample preserves, and the summary reads the count.
    refused_count: int = 0
    unreachable_count: int = 0
    #: Entries that vanished between the listing and their open with nothing in the bucket:
    #: candidates only, and not a verdict. Whether one matters depends on the captured
    #: index, which the authority phase reads -- so this list is informational and
    #: ``gone_referenced`` is what decides anything.
    gone_undurable: list[tuple[str, str]] = field(default_factory=list)
    gone_undurable_count: int = 0
    #: The ``gone_undurable`` entries the CAPTURED authority pair actually names. These
    #: decide the pair like a refusal rather than like an unreachable entry, and the reason
    #: is the premise the whole skew argument rests on: an index OLDER than its bytes is
    #: harmless only because every slot it names already has bytes in the bucket. The pair
    #: is captured BEFORE the enumeration, so it still names a conversation deleted during
    #: the cycle -- and when that conversation was created and deleted inside one interval,
    #: no earlier cycle uploaded it, so the committed index would name a slot the bucket has
    #: never held. The front then fetches an absent object and reads it as a conversation
    #: that never had history.
    #:
    #: Membership in the captured index is the whole test, not the disappearance. A
    #: conversation the index does not name is not something the pair can send a reader to,
    #: so racing its deletion stays the non-failure it has always been -- which is the
    #: routine case, since an owner deleting a conversation is ordinary use.
    #:
    #: Withholding here cannot freeze the index, which is what separates it from an
    #: unreachable entry: this verdict is a race WITHIN one cycle, not a shape on disk. A
    #: file that is genuinely deleted is not listed by the next cycle at all, so it cannot
    #: be gone again, and the pair publishes on that next cycle.
    gone_referenced: list[str] = field(default_factory=list)
    gone_referenced_count: int = 0
    #: Authority-phase keys this cycle did not publish, so they stay as the last complete
    #: cycle left them: the pair when a transcript in the same cycle was refused, and the
    #: generation pointer when the pair is whole but the pointer itself could not be sent
    #: on an interval cycle. On the final cycle an unsent pointer is a refusal, not this.
    withheld: list[str] = field(default_factory=list)
    #: Set when the commit was refused because a NEWER task incarnation is committed -- this
    #: task has been superseded by a replacement. Distinct from an ordinary refusal because
    #: the remedy is distinct: an ordinary refusal is retried next cycle, but a superseded
    #: task can NEVER commit again (the committed incarnation only moves further ahead), so
    #: retrying is the appearance of durability while its front keeps accepting turns no
    #: cycle will ever save. :func:`run_cycle` raises :class:`SidecarSuperseded` on it so the
    #: process ends non-zero and the supervisor tears the task down, exactly as it does for a
    #: bucket that cannot be written.
    superseded: bool = False

    def record_uploaded(self, key: str) -> None:
        """Count an uploaded key, keeping its name only while under the sample cap."""
        self.uploaded_count += 1
        if len(self.uploaded) < _RESULT_SAMPLE_CAP:
            self.uploaded.append(_bounded_identifier(key))

    def record_unchanged(self, key: str) -> None:
        """Count an unchanged key, keeping its name only while under the sample cap."""
        self.unchanged_count += 1
        if len(self.unchanged) < _RESULT_SAMPLE_CAP:
            self.unchanged.append(_bounded_identifier(key))

    @staticmethod
    def _bounded_reason(reason: str) -> str:
        return _bounded_reason(reason)

    def record_refused(self, name: str, reason: str) -> None:
        """Count a refusal, keeping a length-bounded sample while under the cap.

        A refusal withholds the authority pair and makes the cycle incomplete; the checks
        read whether ANY refusal is present, which the sample preserves, so a bounded sample
        plus the exact count carries every decision while a pathological run -- every archived
        segment failing its upload -- cannot retain the whole inventory here.
        """
        self.refused_count += 1
        if len(self.refused) < _RESULT_SAMPLE_CAP:
            self.refused.append((_bounded_identifier(name), self._bounded_reason(reason)))

    def record_unreachable(self, name: str, reason: str) -> None:
        """Count an unreachable entry, keeping a length-bounded sample while under the cap."""
        self.unreachable_count += 1
        if len(self.unreachable) < _RESULT_SAMPLE_CAP:
            self.unreachable.append((_bounded_identifier(name), self._bounded_reason(reason)))

    def record_gone(self, name: str) -> None:
        """Count a gone object, keeping its name only while under the sample cap."""
        self.gone_count += 1
        if len(self.gone) < _RESULT_SAMPLE_CAP:
            self.gone.append(_bounded_identifier(name))

    def record_above_ceiling(self, key: str) -> None:
        """Count an over-ceiling object, keeping its key only while under the sample cap."""
        self.above_ceiling_count += 1
        if len(self.above_ceiling) < _RESULT_SAMPLE_CAP:
            self.above_ceiling.append(_bounded_identifier(key))

    def record_gone_undurable(self, name: str, key: str) -> None:
        """Count a vanished-and-undurable candidate, keeping a sample while under the cap.

        A candidate only, not a verdict -- :meth:`record_gone_referenced` is what the withhold
        decision reads. Bounded like the rest because the archive can put one entry per segment
        through here on a pathological run.
        """
        self.gone_undurable_count += 1
        if len(self.gone_undurable) < _RESULT_SAMPLE_CAP:
            self.gone_undurable.append((_bounded_identifier(name), _bounded_identifier(key)))

    def record_gone_referenced(self, name: str) -> None:
        """Count a vanished object the captured index names, keeping a sample under the cap.

        This is the verdict the withhold decision reads -- via ``gone_referenced_count``, which
        stays exact so a run that overflowed the sample still withholds the pair.
        """
        self.gone_referenced_count += 1
        if len(self.gone_referenced) < _RESULT_SAMPLE_CAP:
            self.gone_referenced.append(_bounded_identifier(name))

    def fold_archive_refusals(self, sink: "_ArchiveWalkSink") -> None:
        """Merge a streamed archive walk's refusals in, preserving its exact counts.

        The sink already holds a capped sample plus a TRUE count; folding it must not shrink
        the count to the sample it carries. So the counts take the sink's true totals and the
        sample entries fill this result's own sample only up to its cap -- a walk that met more
        faults than either cap keeps a bounded sample and the exact number, never one entry per
        fault.
        """
        self.refused_count += sink.refused_count
        for name, reason in sink.refused:
            if len(self.refused) >= _RESULT_SAMPLE_CAP:
                break
            self.refused.append((_bounded_identifier(name), self._bounded_reason(reason)))
        self.unreachable_count += sink.unreachable_count
        for name, reason in sink.unreachable:
            if len(self.unreachable) >= _RESULT_SAMPLE_CAP:
                break
            self.unreachable.append((_bounded_identifier(name), self._bounded_reason(reason)))

    @property
    def complete(self) -> bool:
        # ``gone_referenced`` counts because it WITHHOLDS the pair: a cycle that did not
        # preserve the index has not done its job, and on the final cycle that verdict is
        # the exit code, which is the only way the loss is announced rather than silent.
        # ``gone_undurable`` does NOT count: it is a candidate list, and a candidate the
        # captured index never named cost the pair nothing. Read the COUNTS, not the sample
        # lists: a run that overflowed a sample cap still had those events and must still be
        # incomplete even if the sample were somehow shorter.
        return (
            self.refused_count == 0
            and self.unreachable_count == 0
            and self.gone_referenced_count == 0
        )

    def summary(self) -> str:
        # Every tally reads the true COUNT, not the sample length, so a run that overflowed a
        # sample cap reports how much it did rather than the cap.
        return (
            f"{self.uploaded_count} uploaded, {self.unchanged_count} unchanged, "
            f"{self.gone_count} gone ({self.gone_referenced_count} of them named by the "
            f"captured index and not in the bucket), "
            f"{self.refused_count} refused, "
            f"{self.unreachable_count} unreachable, "
            f"{len(self.withheld)} authority withheld"
        )


@dataclass(frozen=True)
class BackupSet:
    """What one cycle should upload, in two phases, and what it already could not reach.

    The two lists are separate because the authority files are POINTERS: they name the
    transcripts, so they are only true once those transcripts are in the bucket. Holding
    them in their own phase is what lets the cycle publish them last, and withhold them
    entirely when a transcript did not make it.

    The refusals belong here rather than being discovered later because some of them are
    decided while LISTING, not while opening: a linked archive directory means a whole
    subtree is not enumerated, and that has to reach the cycle as a refusal. A set that
    returned only items would report a short cycle as a complete one.

    ``refused`` and ``unreachable`` carry the LIVE and AUTHORITY refusals, complete when the
    set is built. The ARCHIVE refusals live on ``archive_sink`` instead, because the archive
    is streamed: its walk runs as ``data`` is consumed, so its refusals are known only once
    that stream is drained. :func:`run_cycle` reads the sink after the upload phase and
    before its withhold check, folding the archive refusals into the same split. Both are
    split exactly as :class:`RefusedEntry` splits them: the cycle withholds the authority
    pair for a refusal a later cycle can get past, and must not for one it cannot, because
    that withholding would never end.

    ``data`` is a lazy iterable rather than a built list: it lists the live transcripts (one
    flat directory, bounded) and STREAMS the archive tree one path at a time, so the upload
    phase's deadline gate is reached with the archive inventory never held in a list at any
    level and stops the cycle mid-stream instead of after the whole inventory is in memory.
    It is re-iterable -- each iteration re-lists the live paths and re-walks the archive
    afresh, resetting ``archive_sink`` -- so a caller that reads it more than once is safe.
    """

    data: Iterable[tuple[str | None, Path]]
    authority: list[tuple[str, Snapshot]]
    authority_gone: list[str]
    refused: list[tuple[str, str]]
    unreachable: list[tuple[str, str]]
    archive_sink: _ArchiveWalkSink
    #: The writer-unique id this cycle's pair is published under, which the pointer commits.
    #: Minted per cycle so two concurrent writers address distinct generations.
    generation_id: str

    def close_authority(self) -> None:
        """Release the authority descriptors, uploaded or not.

        The withheld path never uploads them, so closing cannot live at the upload site.
        """
        for _key, snapshot in self.authority:
            snapshot.close()


def _name_unreached_remainder(
    current: Path, rest: Iterator[tuple[str | None, Path]], reason: str
) -> list[tuple[str, str]]:
    """Name the not-attempted *current* object plus a BOUNDED sample of what follows it.

    Called when a stop or a deadline gate fires mid-stream. *rest* is the tail of a lazy
    iterator whose end is the archive tree, and retention-off lets that tree grow without
    bound. Draining it to name every unreached object would rebuild the whole inventory in a
    list at the one moment the cycle is trying to STOP -- the same unbounded-retention hazard
    the streamed enumeration removes from the walk. So this pulls at most
    ``_UNREACHED_SAMPLE_CAP`` names and then stops, recording one count-free remainder marker
    when the tail runs past the cap rather than walking it to its end.

    Every entry returned is a refusal, so the cycle is incomplete whether the remainder was
    named whole or summarised -- the current object alone already carries that. A small
    remainder (every real cycle's) fits under the cap and is named in full; only a
    pathological tree is summarised, which is exactly the case that must not be materialised.
    """
    named: list[tuple[str, str]] = [(current.name, reason)]
    for _key, path in rest:
        if len(named) >= _UNREACHED_SAMPLE_CAP:
            # Do not touch *rest* again: advancing it once more is one step further into the
            # tail this cap exists to leave unwalked. The marker stands for "and the rest",
            # count-free because a count is a drain.
            named.append((_UNREACHED_REMAINDER_NAME, reason))
            break
        named.append((path.name, reason))
    return named
