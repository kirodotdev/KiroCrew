"""FolderWatcher -- recursive directory scanner for folder-type knowledge sources."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

from .chunker import CHUNK_TOKEN_SIZE, MAX_CHUNKS_PER_FILE
from .dedup import dedup_document
from .ingestion import DUPLICATE_JOB_STATUS
from .kiroignore import KIROIGNORE_FILENAME
from .kiroignore import load as load_kiroignore
from .readers import FileReader

logger = logging.getLogger(__name__)

# Build-output dirs are excluded because a rebuild deletes/recreates hundreds of
# hashed chunk files at once; indexing that churn drives a bulk delete pass whose
# per-file graph reload runs on the event loop and can stall it past the loop
# watchdog (dist/assets/*.js was the motivating case). Dot-dirs (.cache, .next,
# .venv) are already pruned separately by the startswith(".") rule in _walk.
# ``cdk.out`` is the same churn with a cost attached rather than a stall: AWS CDK
# rewrites hashed asset bundles and template JSON on every synth, so each build
# presents megabytes of generated JSON as changed content and the scan pays a
# fresh extraction call per chunk. Its name only CONTAINS a dot, so the
# dot-prefix rule never prunes it, and the bare ``out`` entry does not match it.
HARD_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                  "dist", "build", "out", "target", "cdk.out"}
DEFAULT_MAX_FILES = 5000

# How many times a file left in 'scanning' is retried before its row is retired to
# 'failed'. The marker means "an attempt started and never reached a terminal state",
# so retrying it is the whole point of crash recovery -- but a retry is not free: it
# re-chunks the file and pays for one model extraction call per chunk plus a document
# summary. A file that interrupts the sweep every time (an input the reader hangs or
# dies on, a kill that always lands in the same place) would therefore be billed
# again on every sweep, forever, with nothing recording that it keeps losing. The cap
# turns that unbounded loop into a bounded one and leaves a 'failed' row the user can
# see and act on.
MAX_SCAN_ATTEMPTS = 3

# How many discovered files a scan processes between ``scan_paused`` re-reads.
# The check used to run per file, i.e. one on-loop sqlite SELECT against
# ``sources`` for every discovered file (up to ``max_files``). Re-reading on this
# interval keeps a mid-scan pause responsive while bounding the query count at
# ceil(files / _PAUSE_RECHECK_FILES) + 1.
_PAUSE_RECHECK_FILES = 100

# Extra skip dirs per source type
SOURCE_TYPE_SKIP_DIRS: dict[str, set[str]] = {
    "obsidian_vault": {".obsidian", ".trash"},
}

# Files that are never real documents, matched case-insensitively against the
# file basename for every folder source type in addition to any per-source
# ignore_patterns. Entries must therefore be lowercase.
#
# Two classes, for two different reasons:
#
# OS-generated temp / lock / junk files carry an otherwise-supported extension,
# so they are discovered, fail ingestion, and -- since failed files are never
# auto-retried -- leave a source permanently stalled below 100%. macOS
# AppleDouble sidecars (``._<name>.docx``) are the common case.
#
# Dependency lock files ingest successfully, which is worse: they are large,
# fully machine-generated, and answer no question a human would ask, yet every
# chunk costs one extraction call and a regenerated lock file is billed again on
# the next sweep. Several of them carry an extension no reader supports today,
# so the glob is what keeps them out regardless of what ``FileReader.SUPPORTED``
# accepts.
DEFAULT_IGNORE_GLOBS: tuple[str, ...] = (
    "._*",            # macOS AppleDouble resource forks
    ".ds_store",      # macOS Finder metadata
    "~$*",            # Microsoft Office lock / owner files
    ".~lock.*",       # LibreOffice lock files
    "thumbs.db",      # Windows thumbnail cache
    "desktop.ini",    # Windows folder settings
    "*.tmp",          # generic temp files
    "*.temp",
    "*.swp",          # vim swap files
    "*.swo",
    "*~",             # editor backup files
    ".#*",            # emacs lock files
    "*.crdownload",   # incomplete browser downloads
    "*.part",
    "*.partial",
    # Dependency lock files: generated resolution output, not documentation.
    "package-lock.json",     # npm
    "npm-shrinkwrap.json",
    "yarn.lock",             # Yarn
    "pnpm-lock.yaml",        # pnpm
    "bun.lockb",             # Bun (binary and text forms)
    "bun.lock",
    "poetry.lock",           # Python
    "uv.lock",
    "pipfile.lock",
    "cargo.lock",            # Rust
    "gemfile.lock",          # Ruby
    "composer.lock",         # PHP
    "packages.lock.json",    # NuGet
    "gradle.lockfile",       # Gradle
    "flake.lock",            # Nix
)


def _prop_str_set(value: object) -> set[str]:
    """Coerce a source property to a set of non-empty strings.

    Source ``properties`` are user-editable JSON, so anything may be in there;
    a bad value must degrade to "no extra entries", never raise mid-scan.
    """
    if not isinstance(value, list):
        return set()
    return {v.strip() for v in value if isinstance(v, str) and v.strip()}


def _prop_extensions(value: object) -> set[str] | None:
    """Coerce the ``include_extensions`` property to a lowercased ``.ext`` set.

    ``None`` (property absent, or not a list) means "no allowlist" -- today's
    behaviour for every source that does not set it. A list means an allowlist,
    INCLUDING an empty one, which allows nothing. Getting that distinction
    backwards would silently change what every existing source ingests.
    """
    if not isinstance(value, list):
        return None
    return {
        e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
        for e in value
        if isinstance(e, str) and e.strip()
    }


def _within(candidate: str, base: str) -> bool:
    """True when *candidate* is *base* or lives underneath it.

    Both sides are normalised (and case-folded where the platform is
    case-insensitive) before comparison, because ``commonpath`` returns a path in
    the platform's own separator form: on Windows a ``/``-separated input comes
    back ``\\``-separated and would never compare equal to the string it was
    derived from. Normalising both is also what keeps a sibling like
    ``/repo-evil`` from passing a naive ``startswith`` test against ``/repo``.
    """
    if not base:
        return True
    norm = os.path.normcase(os.path.normpath(base))
    try:
        return os.path.normcase(
            os.path.commonpath([os.path.normpath(candidate), norm])) == norm
    except ValueError:
        # Different drives on Windows -- definitionally not contained.
        return False


def _prop_int(value: object) -> int:
    """Coerce a non-negative integer source property; 0 when absent or unusable.

    Floats are accepted because a source may already carry one, but a non-finite
    one is not: ``inf`` (which is what ``1e309`` parses to) survives a ``> 0``
    test and then raises in ``int()``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return int(value) if value > 0 else 0


def _rel_posix(rel_dir: str, name: str) -> str:
    """Join a walk-relative directory and an entry name into a ``/``-separated path.

    ``.kiroignore`` patterns are written with ``/``, so a Windows ``\\``-separated
    relative path has to be normalised or every pattern carrying a separator
    silently never matches.
    """
    if rel_dir in ("", "."):
        return name
    return f"{rel_dir.replace(os.sep, '/')}/{name}"


#: Per-source property that overrides the configured folder budget. Separate from
#: the config knob so one oversized folder can be paced harder (or, at 0, let run
#: unbounded) without changing the default every other folder gets.
CHUNK_BUDGET_PROP = "chunk_budget"


def walk_filters(props: dict, source_type: str = "local_folder") -> dict:
    """``FolderWatcher._walk`` keyword arguments for a source's properties.

    Shared with any caller that walks a folder outside a scan -- the add-source
    preview does -- so the files it counts are the files a sweep would take. A
    looser set there reports a count for files the scan then skips.
    """
    return {
        "ignore_patterns": props.get("ignore_patterns", []),
        "extra_skip_dirs": (SOURCE_TYPE_SKIP_DIRS.get(source_type, set())
                            | _prop_str_set(props.get("extra_skip_dirs"))),
        "include_extensions": _prop_extensions(props.get("include_extensions")),
        "min_size": _prop_int(props.get("min_file_bytes")),
        "confine_to_root": bool(props.get("confine_to_root")),
    }


def folder_chunk_budget(props: dict) -> int | None:
    """Chunks a hand-added folder source may ingest in one sweep; ``None`` = unbounded.

    Ingestion costs one LLM extraction call per chunk plus one summary call per
    file, on a pool of billed sessions, so an unpaced first scan of a source-code
    repository spends real money in minutes with nobody watching. The budget does
    not drop any file: the scan takes newest-first until the budget is reached and
    the rest resume on the next sweep from their ``folder_file_state`` rows.

    A per-source ``chunk_budget`` of 0 means "no bound" -- the explicit opt-out for
    a user who does want the whole folder in one burst. Absent, the configured
    default applies, and a configured 0 likewise removes the bound.

    ``properties`` is user-editable JSON reachable from the add-source request
    body, so an override of any other shape -- a float (``1e309`` parses to
    ``inf``, whose ``int()`` raises), a bool (an ``int`` subclass, so ``True``
    would read as a budget of 1), a string, a negative -- means "use the
    configured default" rather than an error. Nothing here may raise: this runs
    inside a request handler and inside a sweep, where a raise is a 500 or a
    skipped scan instead of a paced one.
    """
    override = props.get(CHUNK_BUDGET_PROP)
    if isinstance(override, int) and not isinstance(override, bool) and override >= 0:
        return override or None
    try:
        configured = int(KiroCrewConfig.load().knowledge.folder_ingest_chunk_budget)
    except Exception:
        # Read per call so the knob is live, matching the rest of the watcher.
        logger.debug("Could not read folder_ingest_chunk_budget", exc_info=True)
        return None
    return max(0, configured) or None


#: Average bytes per whitespace-separated word, including the separator.
_BYTES_PER_WORD = 6


def _estimated_chunks(size: int) -> int:
    """Chunks the chunker would produce for a file of *size* bytes.

    Derived from the chunker's own target rather than measured: a chunk targets
    ``CHUNK_TOKEN_SIZE`` tokens, ``_word_count`` approximates 1.3 tokens per word,
    and prose averages roughly ``_BYTES_PER_WORD`` bytes per word including its
    separator. Reading and chunking every file to be exact would cost the walk
    what the scan itself costs, and this number exists to show a user the order of
    magnitude before they commit -- so it is an estimate, never a bound the scan
    enforces.
    """
    words_per_chunk = max(1, int(CHUNK_TOKEN_SIZE / 1.3))
    chunks = -(-max(size, 1) // (words_per_chunk * _BYTES_PER_WORD))
    return min(chunks, MAX_CHUNKS_PER_FILE)


def max_files_prop(props: dict) -> int:
    """A source's ``max_files`` cap, falling back to :data:`DEFAULT_MAX_FILES`.

    Coerced because ``properties`` is user-editable JSON and the value is used in
    arithmetic; a non-positive, non-numeric or non-finite entry means "unset", not
    "cap at 0". ``inf`` has to be excluded explicitly -- it passes a ``> 0`` test
    and then raises in ``int()``.
    """
    value = props.get("max_files", DEFAULT_MAX_FILES)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return DEFAULT_MAX_FILES
    if isinstance(value, float) and not math.isfinite(value):
        return DEFAULT_MAX_FILES
    return int(value)


def estimate_scan_cost(discovered: list[tuple[str, float]], *,
                       max_files: int = DEFAULT_MAX_FILES) -> dict:
    """What a first full scan of *discovered* would cost, in files and LLM calls.

    Mirrors the sweep's own newest-first ordering and file cap so the numbers
    describe the files that would actually be ingested, not everything the walk
    saw. ``llm_calls`` counts one extraction call per chunk plus the per-file
    source-summary call.
    """
    ordered = sorted(discovered, key=lambda f: f[1], reverse=True)
    capped = max(0, len(ordered) - max_files)
    ordered = ordered[:max_files]
    chunks = 0
    for path, _ in ordered:
        try:
            chunks += _estimated_chunks(os.path.getsize(path))
        except OSError:
            # A file that vanished between the walk and this stat contributes
            # nothing; the scan will not find it either.
            continue
    return {"files": len(ordered), "capped": capped, "chunks": chunks,
            "llm_calls": chunks + len(ordered)}


class FolderWatcher:
    """Recursively scans a directory source, tracks file state, triggers ingestion."""

    def __init__(self, store, pipeline):
        self.store = store
        self.pipeline = pipeline
        self._locks: dict[str, asyncio.Lock] = {}

    async def scan_source(self, source: dict, *, chunk_budget: int | None = None) -> dict:
        """Scan a folder source. Returns {new, changed, deleted, skipped, capped}.

        ``chunk_budget`` stops the scan once that many chunks have been ingested
        in THIS sweep, leaving the rest for later sweeps. ``None`` is unbounded.
        Callers resolve the value: :func:`folder_chunk_budget` for a hand-added
        folder, ``knowledge.auto_ingest_chunk_budget`` for an auto-registered one.
        """
        source_id = source["id"]
        if source_id not in self._locks:
            self._locks[source_id] = asyncio.Lock()
        async with self._locks[source_id]:
            return await self._do_scan(source, chunk_budget=chunk_budget)

    async def _do_scan(self, source: dict, *, chunk_budget: int | None = None) -> dict:
        source_id = source["id"]
        uri = source["uri"]
        props = json.loads(source.get("properties") or "{}") if isinstance(
            source.get("properties"), str) else (source.get("properties") or {})

        if not Path(uri).is_dir():
            logger.warning("Folder source %s path missing: %s", source_id, uri)
            return {"error": f"Directory not found: {uri}"}

        max_files = props.get("max_files", DEFAULT_MAX_FILES)
        source_type = source.get("source_type", "local_folder")
        filters = walk_filters(props, source_type)

        # 1. Discover files
        discovered = await asyncio.to_thread(self._walk, uri, **filters)

        # Capture all discovered paths before cap (for accurate deletion detection)
        all_discovered_paths = {fp for fp, _ in discovered}

        # Newest first, unconditionally: it is the order both the file cap and the
        # chunk budget need, so the most recently touched documents are the ones
        # that land when either bound truncates the sweep.
        discovered.sort(key=lambda f: f[1], reverse=True)

        # 2. Enforce cap
        capped = 0
        if len(discovered) > max_files:
            capped = len(discovered) - max_files
            discovered = discovered[:max_files]
            logger.warning("Source %s: capped at %d files (%d skipped)", source_id, max_files, capped)

        # 3. Load existing state
        existing = self._load_state(source_id)
        now = datetime.now().isoformat()

        stats: dict[str, int] = {"new": 0, "changed": 0, "deleted": 0, "skipped": 0, "capped": capped, "failed": 0}
        ingested_paths: list[str] = []  # files (re)ingested this scan, for targeted dedup
        chunks_ingested = 0  # against chunk_budget

        # 4. Detect deleted files (use full set, not capped set, to avoid false deletions)
        for file_path in list(existing.keys()):
            if file_path not in all_discovered_paths:
                await self._handle_deleted(source_id, file_path, existing[file_path])
                stats["deleted"] += 1

        # 5. Process discovered files
        namespace = props.get("namespace", "default")
        # Pause is a user-driven cancel, so it must still land mid-scan — but
        # re-reading ``sources.properties`` once per file cost one on-loop sqlite
        # SELECT per discovered file (up to max_files, 10,000 by default). Check
        # once up front, then only every _PAUSE_RECHECK_FILES files: a pause
        # still takes effect within a bounded number of files instead of costing
        # a query per file.
        paused = self._is_paused(source_id)
        # ``last_seen`` touches for unchanged files are accumulated and flushed
        # as a single executemany instead of one UPDATE per file. They carry no
        # per-row logic and were already committed in the batch commit below, so
        # deferring them to the flush changes nothing an in-scan reader can see.
        last_seen_batch: list[tuple[str, str, str]] = []
        for idx, (file_path, mtime) in enumerate(discovered):
            if idx and idx % _PAUSE_RECHECK_FILES == 0:
                paused = self._is_paused(source_id)
            if paused:
                self._flush_last_seen(last_seen_batch)
                self.store.db.commit()
                return {**stats, "status": "paused"}

            state = existing.get(file_path)

            # 'skipped' and 'failed' need the user to act, so they stay out of the
            # scan until they do.
            if state and state.get("status") in ("skipped", "failed"):
                stats["skipped"] += 1
                continue

            # 'deduped' means the content was already in the Library when this file
            # was last seen -- a statement about that content, not a defect in the
            # file, so it is gated on mtime exactly like 'done'. An unchanged file
            # costs nothing; an edited one is reconsidered and lands if its new
            # content is unique. Skipping it unconditionally would strand it: the
            # marker outlives the copy that caused it, so the file could never be
            # indexed again even after the other copy was deleted.
            if (state and state.get("status") in ("done", "deduped")
                    and mtime <= state.get("mtime", 0)):
                # Unchanged — just update last_seen
                last_seen_batch.append((now, source_id, file_path))
                continue

            # mtime changed or new/interrupted file — check content hash
            content_hash = await asyncio.to_thread(self._hash_file, file_path)
            if not content_hash:
                stats["skipped"] += 1
                continue

            # A row still holding 'scanning' belongs to an attempt that never reached
            # a terminal state -- the sweep was interrupted (process exit, task
            # cancellation) between the marker below and the write that would have
            # replaced it. Retrying is correct, but each retry re-ingests the file at
            # full cost, so the retries are counted and the row is retired once the
            # budget is spent.
            #
            # The budget belongs to the VERSION that kept failing, not to the path:
            # the hash on the interrupted row identifies what was being ingested.
            # Equal hashes mean the same attempt is about to be repeated, so the cap
            # applies. A DIFFERENT hash is a document that has never been tried, and
            # it starts with a full budget -- retiring it would strand new content
            # behind a retirement earned by content the user has already replaced.
            # This is why the hash is read first: the decision cannot be made without
            # it, and it costs one read on the single sweep that retires the row
            # (later sweeps take the 'failed' gate above and read nothing).
            prior_attempts = int(state.get("attempts") or 0) if state else 0
            if state and state.get("status") == "scanning":
                if content_hash != state.get("content_hash"):
                    prior_attempts = 0
                elif prior_attempts >= MAX_SCAN_ATTEMPTS:
                    # Retirement is terminal, so it clears the count like every other
                    # terminal write: what keeps the file out of later sweeps is the
                    # 'failed' gate above, not a spent budget. Carrying the exhausted
                    # count onto the row would make the user's retry -- which clears
                    # the status but not the count -- re-enter the scan already over
                    # budget and be retired again by the very next sweep.
                    self._update_state(
                        source_id, file_path, content_hash, mtime,
                        state.get("item_ids", "[]") or "[]", now, "failed",
                        f"ingestion did not complete after {MAX_SCAN_ATTEMPTS} attempts",
                        commit=False)
                    stats["failed"] += 1
                    continue

            if state and state.get("status") == "done" and content_hash == state.get("content_hash"):
                # Touched but content unchanged
                self._update_state(source_id, file_path, content_hash, mtime, state.get("item_ids", "[]"), now, "done", commit=False)
                continue

            # A row that owned nothing holds a claim for its PREVIOUS content. The
            # file has changed, so that claim now points at the wrong document and
            # has to go before the new content lands.
            if state:
                self.store.release_stale_claim(
                    source_id, state.get("content_hash"), content_hash,
                    json.loads(state.get("item_ids", "[]") or "[]"),
                    state.get("text_hash"))

            # New or changed file — ingest
            if state and state.get("status") == "done":
                old_ids = json.loads(state.get("item_ids", "[]"))
                stats["changed"] += 1
            else:
                old_ids = json.loads(state.get("item_ids", "[]")) if state else []
                if not state:
                    stats["new"] += 1

            # Mark scanning before processing (crash recovery: scanning = interrupted).
            # The incremented attempt count rides along, so the row itself carries how
            # much of its retry budget is left even though nothing else in this sweep
            # survives an abrupt exit.
            self._update_state(source_id, file_path, content_hash, mtime,
                               json.dumps(old_ids), now, "scanning",
                               attempts=prior_attempts + 1)

            item_ids, outcome = await self._ingest_file(
                file_path, source_id, namespace, props, old_ids, root=uri)
            if item_ids is None:
                # Ingestion failed. The 'scanning' marker above is only a crash hint,
                # so it has to be replaced with a terminal status here rather than
                # left to whichever branch inside _ingest_file returned: a row that
                # keeps the marker is re-ingested, at full cost, on every later sweep.
                # Writing it from the caller also restores the content hash and mtime
                # the marker carried, which is what lets the UI say WHICH version of
                # the file failed. The reason recorded by _ingest_file is preserved.
                self._update_state(
                    source_id, file_path, content_hash, mtime, json.dumps(old_ids),
                    now, "failed", self._current_error(source_id, file_path),
                    commit=False)
                stats["failed"] += 1
            elif outcome == "deduped":
                # Refused by the pre-ingest gate: this exact content is already in
                # the Library under another source. 'deduped' -- the same status the
                # dedup sweep writes -- records WHY the file has no item group, so a
                # later scan can tell it apart from an ingest that produced nothing.
                # The content_hash and mtime are stored with it, which is what lets
                # an edit bring the file back into the scan.
                self._update_state(source_id, file_path, content_hash, mtime, "[]", now,
                                   "deduped", commit=False)
                stats["skipped"] += 1
            else:
                self._update_state(source_id, file_path, content_hash, mtime, json.dumps(item_ids), now, "done", commit=False)
                ingested_paths.append(file_path)
                if chunk_budget:
                    chunks_ingested += len(item_ids)
                    if chunks_ingested >= chunk_budget:
                        # Stop this sweep. Files not reached simply have no state
                        # row (or keep their old one), so the next sweep resumes
                        # from them -- the existing status column already carries
                        # the resume point and no extra bookkeeping is needed.
                        stats["chunks"] = chunks_ingested
                        stats["budget_reached"] = 1
                        break

        self._flush_last_seen(last_seen_batch)
        self.store.db.commit()  # Batch commit for all non-crash-recovery updates
        # Targeted cross-source dedup for each newly ingested/changed file, so a folder
        # copy collapses any matching one-shot upload. O(k*n) over the k changed files
        # rather than a full O(n^2) corpus sweep (knowledge/dedup.py).
        if getattr(self.pipeline, "_dedup_enabled", True):
            for fpath in ingested_paths:
                try:
                    dedup_document(self.store, source_id, file_path=fpath, apply=True)
                except Exception:
                    logger.debug("Per-file dedup skipped for %s", fpath, exc_info=True)
        return stats

    def _walk(self, root: str, ignore_patterns: list[str], extra_skip_dirs: set[str],
              include_extensions: set[str] | None = None,
              min_size: int = 0,
              confine_to_root: bool = False) -> list[tuple[str, float]]:
        """Walk directory, return [(file_path, mtime)] for supported files.

        ``include_extensions`` NARROWS what is taken; it can never widen it. A
        value of ``None`` means "no extension allowlist" -- every reader-supported
        extension is taken, which is the behaviour of every source that does not
        set the property. An EMPTY set means "no extension is allowed", so
        nothing is taken; the two must not be conflated, or setting the property
        would silently change what an existing source ingests.

        ``min_size`` drops files below a byte floor, using the ``stat`` this walk
        already performs for the mtime rather than a second syscall.

        ``confine_to_root`` drops a file whose RESOLVED path lands outside *root*.
        ``os.walk`` does not descend directory symlinks, but a FILE symlink is
        followed on open, so a tracked document that is itself a symlink pointing
        outside the registered root otherwise gets that external file indexed. It is off by default because for a folder the user
        registered by hand, following a link they put there is their choice; it is
        on for an auto-registered source, where nobody confirmed the scope and
        content from outside the registered tree is never what was asked for.
        """
        supported = FileReader.SUPPORTED
        results = []
        skip_dirs = HARD_SKIP_DIRS | extra_skip_dirs
        # Root-level rules only, re-read each sweep so editing the file takes effect
        # without touching the source's properties.
        kiroignore = load_kiroignore(root)
        root_real = ""
        if confine_to_root:
            try:
                root_real = str(Path(root).resolve())
            except OSError:
                return []

        for dirpath, dirnames, filenames in os.walk(root):
            rel_dir = os.path.relpath(dirpath, root)
            # Prune skip dirs in-place.
            # Windows: the `.`-prefix hidden check is POSIX-centric — Windows marks
            # hidden via the NTFS hidden attribute, not a dotfile name, so some
            # dirs that are hidden on Windows aren't pruned (benign over-ingestion).
            # Tracked as follow-on work.
            dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
            if kiroignore is not None:
                # Excluded directories are PRUNED, not filtered per file, so a huge
                # generated tree (cdk.out, coverage output) is never descended.
                dirnames[:] = [
                    d for d in dirnames
                    if not kiroignore.is_ignored(_rel_posix(rel_dir, d), is_dir=True)]

            for fname in filenames:
                # The rule file is configuration, not a document. Its extensionless
                # name is in FileReader.SUPPORTED, so it would otherwise be indexed.
                if rel_dir == "." and fname == KIROIGNORE_FILENAME:
                    continue
                # Skip OS-generated temp/lock/junk files (basename, case-insensitive)
                if any(fnmatch(fname.lower(), pat) for pat in DEFAULT_IGNORE_GLOBS):
                    continue
                rel_path = os.path.join(rel_dir, fname) if rel_dir != "." else fname
                # Patterns are written with "/" separators, so the path has to be
                # normalized before matching or every pattern containing a
                # separator silently never matches on Windows.
                if any(fnmatch(rel_path.replace(os.sep, "/"), pat) for pat in ignore_patterns):
                    continue
                if kiroignore is not None and kiroignore.is_ignored(
                        _rel_posix(rel_dir, fname), is_dir=False):
                    continue
                # Extension filter
                ext = Path(fname).suffix.lower()
                if ext not in supported and ext != ".canvas":
                    continue
                if include_extensions is not None and ext not in include_extensions:
                    continue
                full_path = os.path.join(dirpath, fname)
                resolved = str(Path(full_path).resolve())
                if is_sensitive_path(resolved):
                    continue
                if confine_to_root and not _within(resolved, root_real):
                    logger.debug("Skipping %s: resolves outside %s", full_path, root_real)
                    continue
                try:
                    st = os.stat(full_path)
                except OSError:
                    continue
                if min_size and st.st_size < min_size:
                    continue
                results.append((full_path, st.st_mtime))

        return results

    def _load_state(self, source_id: str) -> dict[str, dict]:
        """Load folder_file_state rows for this source."""
        rows = self.store.db.execute(
            "SELECT file_path, content_hash, text_hash, mtime, item_ids, last_seen, status, error_message, attempts FROM folder_file_state WHERE source_id = ?",
            (source_id,)).fetchall()
        return {r["file_path"]: dict(r) for r in rows}

    def _current_error(self, source_id: str, file_path: str) -> str | None:
        """The reason already recorded on this row, if any.

        The failure branches inside ``_ingest_file`` write the specific cause; the
        caller then re-writes the row to make the status transition terminal, and
        reads the cause back so replacing the row does not discard it.
        """
        row = self.store.db.execute(
            "SELECT error_message FROM folder_file_state "
            "WHERE source_id = ? AND file_path = ?", (source_id, file_path)).fetchone()
        return row["error_message"] if row else None

    def _update_state(self, source_id: str, file_path: str, content_hash: str, mtime: float, item_ids: str, now: str, status: str = "done", error_message: str | None = None, *, attempts: int = 0, commit: bool = True):
        # Record the EXTRACTED-TEXT hash alongside the file-bytes one. Ownership
        # lookups have to relate this row to items, and items are keyed by the text
        # hash -- for a PDF or HTML file that is a different string from the bytes
        # hash this row stores for change detection. Derived from the row's own items
        # (one document's items share its hash) so nothing has to be plumbed through
        # the ingest path, and left alone when the row owns nothing: there is then
        # nothing to derive it from, and guessing is what makes documents collide.
        text_hash: str | None = None
        try:
            ids = json.loads(item_ids or "[]")
        except (TypeError, ValueError):
            ids = []
        if ids:
            row = self.store.db.execute(
                "SELECT content_hash FROM items WHERE id = ?", (ids[0],)).fetchone()
            if row:
                text_hash = row["content_hash"]
        elif status == "deduped" and content_hash:
            # A row refused by the pre-ingest gate owns nothing, so it has no items to
            # derive the text hash from -- and it is exactly the row that later needs
            # one, because releasing its claim is what stops a folder being handed a
            # document whose file is gone. Take it from the byte-identical row it was
            # refused against: equal bytes through the same reader give equal text, so
            # this is derived rather than guessed.
            sib = self.store.db.execute(
                "SELECT text_hash FROM folder_file_state "
                "WHERE content_hash = ? AND text_hash IS NOT NULL LIMIT 1",
                (content_hash,)).fetchone()
            if sib:
                text_hash = sib["text_hash"]
            # Left NULL when there is no sibling to derive from. The ownership lookup
            # coalesces to content_hash for such a row, which is the right answer
            # wherever it can be reached: the gate can only have refused a plaintext
            # file in that situation, and for plaintext the two hashes are equal.
        # ``attempts`` defaults to 0, so every terminal write ('done', 'deduped',
        # 'failed') clears the retry budget as a side effect of not passing it: the
        # count only ever accumulates across consecutive 'scanning' markers, which is
        # what it is meant to bound.
        self.store.db.execute(
            "INSERT OR REPLACE INTO folder_file_state (source_id, file_path, content_hash, text_hash, mtime, item_ids, last_seen, status, error_message, attempts) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_id, file_path, content_hash, text_hash, mtime, item_ids, now, status, error_message, attempts))
        if commit:
            self.store.db.commit()

    def _update_last_seen(self, source_id: str, file_path: str, now: str):
        self.store.db.execute(
            "UPDATE folder_file_state SET last_seen = ? WHERE source_id = ? AND file_path = ?",
            (now, source_id, file_path))

    def _flush_last_seen(self, batch: list[tuple[str, str, str]]):
        """Apply accumulated ``(now, source_id, file_path)`` last_seen touches.

        One executemany instead of one execute per unchanged file. Clears
        *batch* so a caller can flush more than once in a scan (the pause
        early-return does).
        """
        if not batch:
            return
        self.store.db.executemany(
            "UPDATE folder_file_state SET last_seen = ? WHERE source_id = ? AND file_path = ?",
            batch)
        batch.clear()

    def _is_paused(self, source_id: str) -> bool:
        """Check if source has scan_paused flag set."""
        row = self.store.db.execute(
            "SELECT properties FROM sources WHERE id = ?", (source_id,)).fetchone()
        if not row:
            return False
        props = row["properties"]
        if isinstance(props, str):
            try:
                props = json.loads(props)
            except Exception:
                return False
        return bool((props or {}).get("scan_paused"))

    async def _handle_deleted(self, source_id: str, file_path: str, state: dict):
        """Archive items for a deleted file and remove state row."""
        item_ids = json.loads(state.get("item_ids", "[]"))
        if item_ids:
            # The file is gone from THIS folder; a copy another source holds survives.
            self.store.delete_items_batch(item_ids, owner_source_id=source_id)
        else:
            # No group to detach -- which is exactly the case for a file that LOST a
            # dedup: its row is 'deduped' with an empty group, while this source is
            # still recorded as a location of the winner's items. That claim is what
            # would later hand this source a document whose file is gone, so it is
            # released by content hash. Nothing is deleted: the items are the winner's.
            # The TEXT hash, not the bytes one: this resolves items, and for a PDF or
            # HTML file the two differ.
            self.store.detach_source_location_by_hash(
                source_id, state.get("text_hash") or "")
        self.store.db.execute(
            "DELETE FROM folder_file_state WHERE source_id = ? AND file_path = ?",
            (source_id, file_path))
        self.store.db.commit()
        logger.info("Deleted file removed: %s (%d items)", file_path, len(item_ids))

    async def _ingest_file(self, file_path: str, source_id: str, namespace: str, props: dict,
                           old_item_ids: list[str],
                           root: str = "") -> tuple[list[str] | None, str]:
        """Ingest one file.

        Returns ``(item_ids, outcome)`` where *outcome* is ``"done"``,
        ``"failed"`` (``item_ids`` is ``None``), or ``"deduped"`` -- the
        pre-ingest gate refused the write because this exact content is already
        in the Library. The caller needs the three cases distinguished because
        they persist different ``folder_file_state`` statuses, and an empty
        ``item_ids`` alone cannot tell "refused" from "ingested nothing".
        """
        # Defense-in-depth: re-resolve symlinks before reading (TOCTOU protection).
        # _walk validated the resolved path, but a symlink can be retargeted between
        # that walk and this read, so BOTH properties are re-checked here: the path
        # must still not be sensitive, and -- when the source is confined -- must still
        # land inside the registered root. Checking only sensitivity would let a
        # retargeted link pull in any non-sensitive file on the host.
        resolved = str(Path(file_path).resolve())
        if props.get("confine_to_root"):
            try:
                root_real = str(Path(root).resolve()) if root else ""
            except OSError:
                root_real = ""
            if not root_real or not _within(resolved, root_real):
                logger.warning(
                    "TOCTOU: path escaped the source root at ingest time: %s -> %s",
                    file_path, resolved)
                sel().log_tool_invocation(
                    session_key="watcher", agent="folder-watcher",
                    tool_name="knowledge.source.file.ingest_denied",
                    outcome="denied",
                    resources=(f"source_id={source_id} file_path={file_path} "
                               f"reason=outside_root_toctou"),
                )
                now = datetime.now().isoformat()
                self._update_state(source_id, file_path, "", 0,
                                   json.dumps(old_item_ids), now, "failed",
                                   "path resolved outside the source root")
                return None, "failed"
        if is_sensitive_path(resolved):
            logger.warning("TOCTOU: sensitive path blocked at ingest time: %s -> %s", file_path, resolved)
            sel().log_tool_invocation(
                session_key="watcher", agent="folder-watcher",
                tool_name="knowledge.source.file.ingest_denied",
                outcome="denied",
                resources=f"source_id={source_id} file_path={file_path} reason=sensitive_path_toctou",
            )
            now = datetime.now().isoformat()
            self._update_state(source_id, file_path, "", 0, json.dumps(old_item_ids), now, "failed", "sensitive path blocked")
            return None, "failed"
        try:
            before_ids = {r["id"] for r in self.store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}

            # Hand the pipeline the path that was just validated, not the one that
            # was validated a moment earlier -- re-deriving it there would reopen the
            # window this check closed. The display name still comes from the logical
            # path, so a symlinked document keeps the name the user sees in the folder.
            job_id = await self.pipeline.ingest_file(
                resolved, source_id=source_id, namespace=namespace,
                original_name=Path(file_path).name,
                old_item_ids=old_item_ids)

            if job_id and (self.pipeline.get_job_status(job_id) or {}).get(
                    "status") == DUPLICATE_JOB_STATUS:
                return [], "deduped"

            # Detect partial failure (pipeline rolls back but doesn't raise)
            row = self.store.db.execute(
                "SELECT sync_status FROM sources WHERE id = ?", (source_id,)).fetchone()
            if row and row["sync_status"] == "error":
                now = datetime.now().isoformat()
                self._update_state(source_id, file_path, "", 0, json.dumps(old_item_ids), now, "failed", "partial ingestion failure")
                return None, "failed"

            after_ids = {r["id"] for r in self.store.db.execute(
                "SELECT id FROM items WHERE source_id = ?", (source_id,)).fetchall()}

            return list(after_ids - before_ids), "done"
        except Exception as e:
            logger.exception("Failed to ingest %s", file_path)
            now = datetime.now().isoformat()
            self._update_state(source_id, file_path, "", 0, json.dumps(old_item_ids), now, "failed", str(e)[:500])
            return None, "failed"

    @staticmethod
    def _hash_file(path: str) -> str | None:
        try:
            resolved = str(Path(path).resolve())
            if is_sensitive_path(resolved):
                return None
            h = hashlib.sha256()
            with open(resolved, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            return h.hexdigest()
        except (OSError, PermissionError):
            return None
