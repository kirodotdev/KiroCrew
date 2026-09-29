"""One catalog row per enumerated skill: metadata, size, deliveries and the ``owned`` hint.

``list_skills`` reads metadata only through the loader's choke-point readers,
reuses a persisted metadata row while its stat fingerprint matches, and reuses
the fingerprints the catalog walk took, so on a process that walked, a warm turn
takes no stat for an unmapped row. Large catalogs are read in bounded batches on a small pool.
Also holds the byte-identical duplicate filter the directory and search apply.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from kiro_crew.skills import SkillsLoader, _ScopedSkillEntry

logger = logging.getLogger("kiro_crew.skills")


def _dedupe_identical_skills(skills: list[dict]) -> list[dict]:
    """Drop later rows that are verified byte-identical copies of an earlier row.

    Two stages, so correctness never rests on a metadata coincidence:

    1. **Candidate fingerprint** — ``(name, description, size_bytes)``, the
       fields a summary line is rendered from, all already loaded by
       ``list_skills()``. No collision (the overwhelmingly common case) means
       no file I/O at all.
    2. **Content verification** — compare the SHA-256 digests captured when
       metadata was safely read, and drop the later row **only when the digests
       match**. Equal-metadata skills whose bodies differ (which the
       pinned path would inject in full) are all kept; an unreadable file is
       kept, never dropped.

    The first row wins, preserving the walk order's operator-installed
    precedence. Confined project rows are exempt entirely: their reads are
    gated through the descriptor-pinned reader, and mirrored-root duplicates
    only arise from unconfined trees anyway.
    """
    seen: dict[tuple[str, str, int], list[dict]] = {}
    out: list[dict] = []
    for s in skills:
        if s.get("confine_root"):
            out.append(s)
            continue
        fp = (str(s.get("name", "")), str(s.get("description", "")), int(s.get("size_bytes") or 0))
        rivals = seen.setdefault(fp, [])
        if rivals:
            this_digest = s.get("content_digest")
            if this_digest and any(r.get("content_digest") == this_digest for r in rivals):
                continue  # verified byte-identical copy of an earlier row
        rivals.append(s)
        out.append(s)
    return out


def _fingerprint_mtime_and_size(fingerprint: str) -> tuple[float | None, int]:
    """Recover ``(mtime, size)`` from a ``dev:ino:ctime_ns:mtime_ns:size`` string.

    Lets a catalog read reuse the stat the WALK already paid instead of taking its
    own. ``(None, 0)`` for anything that does not parse, which sends the caller
    down the ordinary stat path rather than serving an invented size — a wrong
    figure here is reported to the user as a skill's injection cost.
    """
    parts = fingerprint.split(":")
    if len(parts) != 5:
        return None, 0
    try:
        return int(parts[3]) / 1_000_000_000, int(parts[4])
    except ValueError:
        return None, 0


def list_skills(
    loader: SkillsLoader,
    project_dir: str | Path | None = None,
    *,
    _entries: list[_ScopedSkillEntry] | None = None,
) -> list[dict]:
    """Return per-skill metadata for the dashboard's Skills page.

    Carries the three fields the injection-cost control needs alongside the
    identity ones: whether the skill opted out of full-body injection, how
    big its body is, and how many times that body was actually DELIVERED into
    a prompt. Cost is the product of the last two, and a user deciding
    whether to opt a skill out cannot weigh it without both.

    ``deliveries`` counts body deliveries, not trigger matches: the ledger
    records only when a body reaches the prompt, so a false-positive match, a
    pointer-only skill, and an undelivered match all count zero. Two
    consequences a caller must not paper over — a skill already opted out
    stops accruing entirely, so its figure is historical and frozen; and this
    is therefore a measure of what was SPENT, never of how often the skill
    was relevant.

    ``deliveries`` is ``None`` when the skill has no ledger entry, which is
    different from zero: an entry can also age out of the 30-day window.

    ``owned`` says whether Kiro Crew may rewrite the file. A skill reached
    through ``skills.extra_paths`` is listed but not ours to edit, so the UI
    must not offer a toggle the endpoint will refuse.

    This is blocking filesystem/SQLite work; async callers must offload it.
    Unconfined rows take exactly one stat and reuse its mtime for
    the frontmatter cache. Confined project rows perform no path stat; their
    size and cache token come from bytes admitted by the no-link reader.
    """
    from kiro_crew import skills as sk  # circular import: the facade imports this module

    started = time.monotonic()
    cached_metadata = loader._search_index.metadata_snapshot() if loader._search_index else {}
    snapshot_done = time.monotonic()
    changed_metadata: list[tuple[str, str, dict]] = []
    skill_reads = 0
    skills: list[dict] = []
    entries = (
        [sk._ScopedSkillEntry(*entry) for entry in loader._iter_visible(project_dir)]
        if _entries is None
        else _entries
    )
    # Fingerprints from a walk THIS PROCESS performed, when it has performed
    # one. They let a warm build skip the stat it would otherwise take purely to
    # decide whether the persisted metadata is still good — an O(N) syscall pass
    # every turn pays on a large tree. Restricted to this process's own walk on
    # purpose: a fingerprint read off disk records what some earlier process
    # saw, so trusting it would make a restart serve a description for a file
    # edited out of band since. A row the walk did not fingerprint — a mapped
    # row, or any row on a process still serving the stored list — stats exactly
    # as before, which is validation rather than discovery: no walk, and no body
    # read.
    fingerprint_hint = loader._catalog_fingerprint_hint(project_dir)
    scanned = time.monotonic()

    def read_entry(
        entry: _ScopedSkillEntry,
    ) -> tuple[dict[str, str], int, str, tuple[str, str, dict] | None, int]:
        name, skill_file, project_root, mapping_root = entry
        fingerprint = ""
        changed = None
        reads = 0
        if project_root is not None:
            meta, size_bytes = loader._confined_frontmatter_and_size(skill_file, project_root)
        else:
            st: os.stat_result | None = None
            # A mapped row's fingerprint carries its mapping root, so the hint
            # (which records the bare stat) would not match what is stored.
            hinted = None if mapping_root else fingerprint_hint.get(str(skill_file))
            if hinted is not None:
                fingerprint = hinted
                mtime, size_bytes = _fingerprint_mtime_and_size(hinted)
            else:
                try:
                    st = skill_file.stat()
                except OSError:
                    st = None
                fingerprint = (
                    f"{st.st_dev}:{st.st_ino}:{st.st_ctime_ns}:{st.st_mtime_ns}:{st.st_size}"
                    if st is not None
                    else ""
                )
                if fingerprint and mapping_root:
                    fingerprint += f":{mapping_root}"
                mtime = st.st_mtime if st is not None else None
                size_bytes = st.st_size if st is not None else 0
            cached = cached_metadata.get(str(skill_file))
            if cached and cached[0] == fingerprint and cached[1].get("_catalog_key") == name:
                meta = cached[1]
            else:
                reads += 1
                loader._fm_cache.pop(str(skill_file), None)
                meta = loader._cached_frontmatter(
                    skill_file,
                    mtime=mtime,
                    within=None,
                    canonical_root=mapping_root,
                )
                meta["_catalog_key"] = name
                if fingerprint:
                    changed = (str(skill_file), fingerprint, meta)
            if mtime is not None and meta:
                loader._fm_cache[str(skill_file)] = (mtime, meta)
        return meta, size_bytes, fingerprint, changed, reads

    def rows() -> Iterator[
        tuple[
            _ScopedSkillEntry,
            tuple[dict[str, str], int, str, tuple[str, str, dict] | None, int],
        ]
    ]:
        if len(entries) < sk._CATALOG_READ_BATCH:
            for entry in entries:
                yield entry, read_entry(entry)
            return
        with ThreadPoolExecutor(
            max_workers=sk._CATALOG_READ_WORKERS, thread_name_prefix="skill-catalog"
        ) as pool:
            for batch in batched(entries, sk._CATALOG_READ_BATCH):
                futures = [pool.submit(copy_context().run, read_entry, entry) for entry in batch]
                yield from zip(batch, (future.result() for future in futures))

    for entry, (meta, size_bytes, fingerprint, changed, reads) in rows():
        name, skill_file, project_root, mapping_root = entry
        if changed is not None:
            changed_metadata.append(changed)
        skill_reads += reads
        if sk._html_skill_refused(meta, skill_file):
            continue
        skills.append(
            {
                # Internal: lets a later re-read (see _rank_key) reuse the root
                # this row was read under instead of guessing at one.
                "confine_root": project_root,
                "mapping_root": mapping_root,
                "key": name,
                "name": meta.get("name", name),
                "description": meta.get("description", name),
                "path": str(skill_file),
                "dir": str(skill_file.parent),
                "always": meta.get("always", "").strip().lower() == "true",
                # Carried so a caller assembling context can drop a
                # repo-scoped skill from the INDEX, not just from the
                # injected body: a summary line the agent is told to read
                # advertises the skill just as effectively. Stripped because
                # the consumer guards on this value's truthiness before
                # calling the gate, so it has to agree with the other two
                # gate call sites about what counts as "no scope at all".
                "repo_scope": meta.get("repo_scope", "").strip(),
                # Mirrors split_triggered: confined project rows always use
                # the body; only an explicit `false` on an unconfined skill
                # opts out. A malformed value therefore reads as injecting.
                "inject_on_trigger": (
                    project_root is not None
                    or meta.get("inject_on_trigger", "").strip().lower() != "false"
                ),
                "size_bytes": size_bytes,
                "fingerprint": fingerprint if project_root is None else "",
                "content_digest": (meta.get("_content_digest", "") if project_root is None else ""),
                "metadata_indexed": project_root is None and bool(fingerprint),
                "deliveries": loader._delivery_count(name),
                "owned": loader._owned_hint(skill_file),
            }
        )
    assembled = time.monotonic()
    if loader._search_index and not loader._search_index.store_metadata(changed_metadata):
        changed_paths = {path for path, _, _ in changed_metadata}
        for row in skills:
            if row["path"] in changed_paths:
                row["metadata_indexed"] = False
    logger.debug(
        "skill catalog: %.2fms total, %.2fms snapshot, %.2fms scan, "
        "%.2fms read/assemble, %.2fms persist, %d rows, %d metadata reads",
        (time.monotonic() - started) * 1000,
        (snapshot_done - started) * 1000,
        (scanned - snapshot_done) * 1000,
        (assembled - scanned) * 1000,
        (time.monotonic() - assembled) * 1000,
        len(skills),
        skill_reads,
    )
    return skills


def _owned_hint(loader: SkillsLoader, skill_file: Path) -> bool:
    """Whether *skill_file* sits under the directory Kiro Crew owns.

    Syscall-free on purpose: this runs once per skill inside ``list_skills``,
    which the event loop calls while assembling the skill index, and
    ``Path.resolve()`` costs a stat each. It is an ADVISORY hint for the UI —
    the authoritative check is the resolved one in
    ``set_inject_on_trigger``, which is the write boundary and runs once per
    toggle. A path that only differs by a symlink therefore reads as owned
    here and is still refused there; the failure mode is a toggle that
    reports an error, never an unowned file being rewritten.
    """
    try:
        return skill_file.is_relative_to(loader._dir)
    except (OSError, ValueError):
        return False


def _delivery_count(loader: SkillsLoader, key: str) -> int | None:
    """Body deliveries recorded for *key*, or ``None`` when untracked.

    Best-effort: the ledger is telemetry, so a missing or unreadable one
    yields ``None`` rather than failing the whole listing.
    """
    if loader._usage is None:
        return None
    try:
        hits, _ = loader._usage.score(key)
    except Exception:
        return None
    return int(hits) if hits else None
