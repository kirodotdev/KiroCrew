"""Audits of what the agent has stored, and of what it may store.

:func:`scan_history` reads recent conversation history for suspicious shell
commands (:func:`audit_bash_command`, matched against ``SUSPICIOUS_BASH_PATTERNS``).
:func:`scan_memory` screens every declared memory store -- its durable rows, their
recorded history and the store's lessons file -- for prompt-injection content, and
reports a store or lessons file it could not read as unauditable rather than clean
(``STORE_UNAUDITABLE``, ``LESSONS_UNAUDITABLE``).
:func:`should_record_observe_history` decides whether an observe-mode message may
be recorded at all.

``kiro_crew.vector_memory`` is imported inside :func:`scan_memory`, never at the top:
it imports the redaction entry points from ``kiro_crew.security`` at ITS top level,
so a top-level import here would be circular -- and the guard around it would
quietly turn the store into ``None`` and disable the scan. Deferring it also keeps
the numpy/faiss/snowballstemmer stack off the lightweight import path.

``kiro_crew.security`` is the import path. This module reads the package's
``_submodule`` resolver and ``logger``, so the package imports it at its foot, once
both are bound. New audits of stored content belong here.
"""

from __future__ import annotations

import fnmatch
import hashlib as _hashlib
import json
from typing import TYPE_CHECKING

from kiro_crew.memory_stores import DEFAULT_MEMORY_STORE
from kiro_crew.memory_stores import declared_store_names as memory_stores_declared_names
from kiro_crew.memory_stores import named_store_of_db, resolve_store_path
from kiro_crew.security import _submodule, logger

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from kiro_crew.vector_memory import VectorMemoryStore


def audit_bash_command(command: str) -> str | None:
    """Check a bash command against suspicious patterns.

    Returns warning string, or None if clean.
    Patterns with ``*`` are matched as globs via fnmatch.
    """
    # Owners of the re-exported names read below, resolved through the
    # import system so the value is read from the one place it lives.
    _denied_rules = _submodule("denied_rules")

    lower = command.lower()
    for pattern in _denied_rules.SUSPICIOUS_BASH_PATTERNS:
        pat = pattern.lower()
        if "*" in pat:
            if fnmatch.fnmatch(lower, f"*{pat}*"):
                return f"Suspicious command detected: matches '{pattern}'"
        elif pat in lower:
            return f"Suspicious command detected: matches '{pattern}'"
    return None


def scan_history(history_dir: Path, last_n: int = 100) -> list[dict]:
    """Scan recent conversation history for suspicious tool usage.

    Returns list of findings: [{file, line, tool, command, warning}]
    """
    findings: list[dict] = []
    if not history_dir.is_dir():
        return findings

    files = sorted(history_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    checked = 0
    for f in files:
        try:
            for line in f.read_text().splitlines():
                if checked >= last_n:
                    return findings
                checked += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = entry.get("content", "")
                role = entry.get("role", "")
                if role != "assistant" or not isinstance(content, str):
                    continue
                # Check for bash commands in tool calls
                warning = audit_bash_command(content)
                if warning:
                    findings.append(
                        {
                            "file": f.name,
                            "warning": warning,
                            "snippet": content[:200],
                        }
                    )
        except OSError:
            continue
    return findings


#: How many episodic rows one store contributes to an injection audit. Newest
#: first (``get_episodic_list`` orders by ``created_at DESC``), so the bound
#: drops the oldest rows rather than a random slice, and it is per store: the
#: work is proportional to the number of declared stores, not shared across them.
_MEMORY_AUDIT_EPISODIC_LIMIT = 1000

#: How much of a matching row's own text a finding carries. Enough to recognise the
#: row and decide what to remove; short enough that a report of many findings stays
#: readable in a terminal. Shared by every tier so one row's excerpt cannot be longer
#: than another's purely by which pass found it.
_MEMORY_AUDIT_VALUE_CHARS = 200


def _memory_stores_to_scan() -> list[tuple[str, Path | None]]:
    """``(store name, vector file)`` for every store :func:`scan_memory` opens.

    The DEFAULT store is FIRST and carries ``None``, meaning "construct
    ``VectorMemoryStore`` with no path". That is not a shortcut: it keeps the
    default store's construction byte-identical, including the side effect that
    a bare ``VectorMemoryStore().init()`` CREATES ``config_dir()/memory.db``
    when it is absent. An install with no named stores must behave exactly as it
    always has, down to that.

    Named stores come off :func:`memory_stores_declared_names`, the enumeration every tier
    of the audit shares.

    A declared store whose vector file does not exist yet is SKIPPED. A named
    store starts empty and its file appears at the first write, while
    ``VectorMemoryStore.init()`` creates the directory, the file and (under the
    crew schema lineage) decides its shape — so scanning one would have an audit
    materialize a silo that holds nothing to scan. Skipping it costs this tier
    only: that store's JSONL lessons tier is audited regardless, and on a
    silo-bound crew with no vector store it is the ONLY populated tier there is.

    Never raises: a config that cannot be read degrades to the default store
    alone, the same floor ``memory_stores._declared_stores`` falls back to.
    """
    stores: list[tuple[str, Path | None]] = [(DEFAULT_MEMORY_STORE, None)]
    for name in memory_stores_declared_names():
        if name == DEFAULT_MEMORY_STORE:
            continue
        try:
            # Opened DIRECTLY, never through the agent file gate: the whole
            # ``memory_stores/`` subtree is a keystone leaf, so
            # ``is_sensitive_path`` is True for every path this resolves. This is
            # the established keystone-reader pattern — a legitimate reader opens
            # the path itself, and relaxing the fence for this one caller would
            # unfence the subtree for every tool caller too.
            path = resolve_store_path(name)
            # Confirm attribution independently of config lookup: each audit row
            # must name the store whose database was actually opened. Strict
            # binding resolution also rejects unavailable or undeclared stores.
            if named_store_of_db(path) != name:
                logger.warning(
                    "memory store %r resolved to %s, which is not that store's own file; "
                    "not audited rather than reporting another store's rows under its name",
                    name,
                    path,
                )
                continue
            from kiro_crew.memory_stores import memory_store_version

            if not path.exists() and memory_store_version(name) != 2:
                logger.warning(
                    "memory store %r has no vector file yet; its vector tier is not audited",
                    name,
                )
                continue
        except Exception:
            logger.warning(
                "memory store %r has no resolvable vector file; not audited", name, exc_info=True
            )
            continue
        stores.append((name, path))
    return stores


#: ``type`` of the synthetic finding that stands in for a store nobody could read.
#: Not an injection match -- it is the audit reporting that it does not KNOW, which is
#: the one answer a security verdict must never round down to "clean".
STORE_UNAUDITABLE = "store_unauditable"


def _unauditable_finding(store_name: str) -> dict:
    """A finding meaning "this store could not be read", shaped like a real one.

    Carries the same four keys both CLI printers read (``type`` / ``key`` / ``warning``
    / ``value``) plus ``store``, so it renders through each of them unchanged. Without
    it a per-store failure is fail-soft all the way to the verdict: ``scan_memory()``
    returns ``[]`` and the CLI prints a green tick, so corrupting one silo would silence
    the audit for that silo AND earn a clean bill of health for the whole install.
    """
    return {
        "type": STORE_UNAUDITABLE,
        "key": store_name,
        "warning": "store could not be read; its contents are UNKNOWN, not clean",
        "value": "",
        "store": store_name,
    }


#: ``type`` of a finding from a store's JSONL lessons tier. Distinct from ``"semantic"``
#: and ``"episodic"`` because the tier decides the remedy — a lesson is removed with
#: ``kirocrew learn remove``, not a memory delete — and because this tier exists on a
#: store that has no vector file at all, where it is the only thing feeding the prompt.
LESSON_FINDING_TYPE = "lesson"

#: ``type`` of the synthetic finding that stands in for a lessons file nobody could read.
#: The lessons twin of :data:`STORE_UNAUDITABLE`, separate so a report says WHICH tier is
#: unknown: a store can have a readable vector file and an unreadable lessons file.
LESSONS_UNAUDITABLE = "lessons_unauditable"


def _unauditable_lessons_finding(store_name: str, path: Path) -> dict:
    """A finding meaning "this store's lessons file could not be read".

    Shaped exactly like :func:`_unauditable_finding` — the four keys both CLI printers
    read plus ``store`` — for the same reason: a read failure that returns no finding is
    fail-soft all the way to the verdict, so making one lessons file unreadable would
    both silence that tier and earn the install a green tick.

    ``key`` names the FILE rather than the store, which is what a reader needs here: the
    store name is already on the ``store`` key, and the actionable fact is which path
    would not open.
    """
    return {
        "type": LESSONS_UNAUDITABLE,
        "key": str(path),
        "warning": "lessons file could not be read; its contents are UNKNOWN, not clean",
        "value": "",
        "store": store_name,
    }


def _lessons_files_to_scan() -> list[tuple[str, Path]]:
    """``(store name, lessons file)`` for every store's JSONL lessons tier.

    The DEFAULT store is first, then each declared store in name order, off the shared
    :func:`memory_stores_declared_names`. Every store is listed, including one with no
    ``memory.db``: a silo-bound crew's lesson WRITES land in this file precisely when
    that silo has no vector store (``dashboard.handlers.cron._lesson_jsonl_store`` routes
    by BINDING, and ``ContextBuilder.get_lessons_for`` creates only the markdown
    directory), and ``LessonStore.get_context`` injects those rows into that crew's
    prompt as ``[Learned corrections]``. So this is the tier an audit of vector files
    alone reports "clean" about while it is the only populated, prompt-injected tier the
    install has.

    Each path comes from :class:`learn.LessonStore` itself rather than from a composed
    ``<dir>/lessons.jsonl``, so the audit reads the exact file the writer writes.

    A named store's directory is taken from the vector path this audit already trusts:
    ``resolve_store_path(name).parent`` is the directory ``ensure_memory_store_dir``
    hands the writer, and ``named_store_of_db`` is the same positive attribution gate the
    vector pass applies — ``resolve_store_path`` DEGRADES rather than raising, so without
    it a config save landing mid-scan would resolve the operator's OWN
    ``lessons.jsonl`` under a crew's name and print the operator's corrections as that
    crew's. The resolved path is re-checked against that directory afterwards because
    ``LessonStore.__init__`` has fallbacks of its own; a store whose file lands outside
    its own directory is not audited rather than misattributed.

    Never raises. A store that cannot be resolved is dropped with a warning, which is
    fail-soft on the ENUMERATION only — a file that resolves and then will not open is a
    finding, not a silence (see :func:`_scan_store_lessons`).
    """
    from kiro_crew.learn import LessonStore

    files: list[tuple[str, Path]] = []
    for name in memory_stores_declared_names():
        try:
            from kiro_crew.memory_stores import memory_store_version

            if memory_store_version(name) == 2:
                continue
            if name == DEFAULT_MEMORY_STORE:
                # No ``base_dir``: byte-identical with the global ``LessonStore()`` every
                # write path constructs, so the default store's file is the one the
                # dashboard route, the CLI and the context builder all share.
                files.append((name, LessonStore().path))
                continue
            db_path = resolve_store_path(name)
            if named_store_of_db(db_path) != name:
                logger.warning(
                    "memory store %r resolved to %s, which is not that store's own file; "
                    "its lessons tier is not audited rather than reporting another "
                    "store's corrections under its name",
                    name,
                    db_path,
                )
                continue
            path = LessonStore(base_dir=db_path.parent).path
            if path.parent != db_path.parent:
                logger.warning(
                    "memory store %r resolved its lessons file to %s, outside the store's "
                    "own directory %s; not audited rather than misattributed",
                    name,
                    path,
                    db_path.parent,
                )
                continue
        except Exception:
            logger.warning(
                "memory store %r has no resolvable lessons file; not audited",
                name,
                exc_info=True,
            )
            continue
        files.append((name, path))
    return files


def _scan_store_lessons(store_name: str, path: Path, findings: list[dict]) -> None:
    """Injection findings for ONE store's lessons file, each attributed to *store_name*.

    *path* is opened DIRECTLY, never through the agent file gate: a named store's
    lessons file is inside the keystone ``memory_stores/`` subtree, so
    ``is_sensitive_path`` is True for it. That is the established keystone-reader pattern
    (see ``docs/system-specs/modules/security.md`` and
    ``docs/architecture/security-deep-dive.md``) — a legitimate reader opens the path
    itself, because relaxing the fence for this one caller would unfence the subtree for
    every tool caller too.

    An ABSENT file is not a finding: a store's lessons tier appears at the first
    correction, so absence is the ordinary state of a fresh store rather than a failure.
    Any OTHER read failure IS one, because the whole file is a directive tier and "I
    could not look" must not render as a tick.

    Screens ``rule`` and ``negative`` — the two fields ``LessonStore.get_context``
    renders into the prompt — with the SAME predicate the vector tiers use, so widening
    the surface does not widen what counts as a finding. Every syntactically valid row is
    screened, including one carrying a ``repo_scope`` that ``load_all`` drops: an audit
    has no project to evaluate a scope against, and poisoned text sitting in this file is
    reportable wherever a context build would have rendered it.

    ONE finding per ROW, on the first matching field. The row is the unit of removal, so
    a row poisoned in both fields is one thing for a reader to act on rather than two
    findings sharing a key and inflating the count.

    Reads the file whole. ``LessonStore`` itself loads and rewrites it whole on every
    save and prunes it to a bounded row count, so the whole file already IS the
    production working set — and a row-count bound here would be a blind spot an
    attacker could append past.

    The excerpt is NOT redacted, matching the vector passes exactly: they surface
    ``value_json`` and ``text`` as stored, and a report that redacted one tier but not
    another would read as though the tiers held different classes of content.
    """
    # Owners of the re-exported names read below, resolved through the
    # import system so the value is read from the one place it lives.
    _helpers = _submodule("helpers")

    try:
        # ``errors="replace"`` so a file carrying a non-UTF-8 byte is still screened
        # rather than reported unauditable: the rows around the bad byte are exactly the
        # ones an attacker would hope a decode error hid. It also means the only failure
        # this can raise is an OSError -- there is no decode path left to fail.
        raw = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        logger.debug("memory store %r has no lessons file at %s", store_name, path)
        return
    except OSError:
        logger.warning(
            "memory store %r has an unreadable lessons file at %s; reporting it as "
            "unauditable rather than clean",
            store_name,
            path,
            exc_info=True,
        )
        findings.append(_unauditable_lessons_finding(store_name, path))
        return
    for lineno, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        matched = next(
            (
                value
                for value in (row.get("rule"), row.get("negative"))
                if isinstance(value, str) and _helpers._contains_injection(value)
            ),
            None,
        )
        if matched is None:
            continue
        findings.append(
            {
                "type": LESSON_FINDING_TYPE,
                # The file and line, so the row can be found and removed. The matching
                # text is the poisoned content itself, so it is the ``value``.
                "key": f"{path.name}:{lineno}",
                "value": matched[:_MEMORY_AUDIT_VALUE_CHARS],
                "warning": "Injection pattern detected",
                "store": store_name,
            }
        )


def _memory_audit_matches(value: object) -> Iterator[str]:
    """Inspect decoded JSON leaves, including JSON stored inside revision snapshots."""
    # Owners of the re-exported names read below, resolved through the
    # import system so the value is read from the one place it lives.
    _helpers = _submodule("helpers")

    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, dict):
            pending.extend(current.keys())
            pending.extend(current.values())
        elif isinstance(current, list):
            pending.extend(current)
        elif isinstance(current, str):
            try:
                decoded = json.loads(current)
            except (ValueError, TypeError):
                decoded = None
            if isinstance(decoded, (dict, list, str)):
                pending.append(decoded)
            elif _helpers._contains_injection(current):
                yield current


def _scan_memory_record_history(
    store: VectorMemoryStore,
    store_name: str,
    findings: list[dict],
    reported: set[tuple[str, bytes]],
) -> None:
    """Audit all metadata and revisions without loading the history into retrieval.

    The tables are optional for older databases. Read every physical row in bounded
    batches: a conflict proposal or a corrected old value is still durable content.
    The same poisoned leaf repeated in an active row and its journal is one finding
    for that record; a different historical payload remains separately reportable.
    """
    for table, kind, identity, prefix in (
        ("memory_record_meta", "metadata", "record_id", ""),
        ("memory_revisions", "revision", "record_id", ""),
        ("memory_history", "history", "day", "history:"),
        ("memory_consolidations", "consolidation", "source_id", "consolidation:"),
    ):
        with store._db_lock:
            if not store.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",  # wokeignore:rule=master
                (table,),
            ).fetchone():
                continue
            # These are code-owned identifiers, never user input.
            cursor = store.db.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
            columns = [column[0] for column in cursor.description]
        while True:
            with store._db_lock:
                rows = cursor.fetchmany(128)
            if not rows:
                break
            for values in rows:
                row = dict(zip(columns, values))
                record_id = prefix + str(row[identity])
                matches = list(_memory_audit_matches(row))
                fresh = [
                    value
                    for value in matches
                    if (record_id, _hashlib.sha256(value.encode()).digest()) not in reported
                ]
                if not fresh:
                    continue
                reported.update(
                    (record_id, _hashlib.sha256(value.encode()).digest()) for value in matches
                )
                key = f"{record_id}@{row['id']}" if "id" in row else record_id
                findings.append(
                    {
                        "type": kind,
                        "key": key,
                        "value": fresh[0][:_MEMORY_AUDIT_VALUE_CHARS],
                        "warning": "Injection pattern detected",
                        "store": store_name,
                    }
                )


def _scan_memory_store(store: VectorMemoryStore, store_name: str, findings: list[dict]) -> None:
    """Injection findings for ONE opened store, each attributed to *store_name*.

    ``store`` is the attribution carrier rather than the caller's own bookkeeping
    because the findings from every store land in ONE flat list: an unattributed
    row reads as the global store's, which both hides which crew's silo was
    poisoned and puts one crew's memory text in another crew's report.

    ``store`` is appended LAST so the four pre-existing keys keep their
    positions; a consumer reading only those sees the shape it always saw.

    Appends into the CALLER's list rather than building and returning its own: a raise
    partway through -- a corrupt page reached on the episodic pass -- would otherwise
    discard every semantic finding already collected for this store along with the
    exception.
    """
    reported: set[tuple[str, bytes]] = set()
    for entry in store.get_all_semantic():
        val = entry.get("value_json", "")
        matches = list(_memory_audit_matches(val))
        if matches:
            reported.update(
                (f"key:{entry['key']}", _hashlib.sha256(value.encode()).digest())
                for value in matches
            )
            findings.append(
                {
                    "type": "semantic",
                    "key": entry["key"],
                    "value": val[:_MEMORY_AUDIT_VALUE_CHARS],
                    "warning": "Injection pattern detected",
                    "store": store_name,
                }
            )
    for entry in store.get_episodic_list(limit=_MEMORY_AUDIT_EPISODIC_LIMIT):
        text = entry.get("text", "")
        matches = list(_memory_audit_matches(text))
        if matches:
            reported.update(
                (entry["id"], _hashlib.sha256(value.encode()).digest()) for value in matches
            )
            findings.append(
                {
                    "type": "episodic",
                    "key": entry["id"],
                    "value": text[:_MEMORY_AUDIT_VALUE_CHARS],
                    "warning": "Injection pattern detected",
                    "store": store_name,
                }
            )
    _scan_memory_record_history(store, store_name, findings, reported)


def scan_memory() -> list[dict]:
    """Scan every declared memory store's durable rows for suspicious content.

    Returns one flat list of findings, each carrying the ``store`` it came from.
    The default store comes first, then each declared named store in name order.

    Covers SQLite facts, directives, episodes, metadata, immutable revisions,
    learned history and consolidation receipts, then V1's JSONL lessons file.
    Historical values are audit-only and are not added to model retrieval. V2
    never consults an old learned-file sidecar as another authority.

    Scanning named stores is not completeness for its own sake either: a crew silo's
    directive tier is loaded into that crew's prompt, so it is the highest-value
    prompt-injection target on disk, and the audit that reported "clean" while
    opening only ``config_dir()/memory.db`` was reporting on a file the attacker
    had no reason to write.

    FAIL SOFT per store and per tier. One unreadable or corrupt silo costs that
    store's findings for that tier and nothing else — not the default store's, not
    those of the stores after it, and not the other tier's — because an audit that
    aborts on the first bad file is an audit an attacker can silence by corrupting one
    silo. The VERDICT is not fail-soft: a tier that could not be read reports itself,
    so "unknown" never renders as a tick.

    The vector tier runs first, whole, then the lessons tier. Grouping by tier rather
    than by store keeps the vector pass's list order untouched, so an install with no
    lessons file reports exactly what it always reported.
    """
    findings: list[dict] = []
    # Lazy import to avoid a circular dependency (vector_memory imports
    # redact_credentials/redact_exfiltration_urls from kiro_crew.security at its top
    # level) and to keep the optional numpy/faiss/snowballstemmer stack off the
    # lightweight import path. Skip the scan cleanly if it is unavailable.
    try:
        from kiro_crew.vector_memory import VectorMemoryStore
    except Exception:  # numpy/faiss/snowballstemmer are optional heavy deps; any
        # import-time failure (ImportError, OSError from a C-extension, etc.)
        # must skip the scan cleanly rather than crash the caller.
        # The lessons tier is stdlib-only and does NOT share that fate: it is the
        # tier a store has when it has no vector store at all, so returning early
        # here would make a missing numpy the way to silence it.
        _scan_lessons_tier(findings)
        return findings

    for store_name, db_path in _memory_stores_to_scan():
        try:
            from kiro_crew.memory_stores import memory_store_version

            member_store = db_path is not None and memory_store_version(store_name) == 2
            if member_store and db_path is not None:
                from kiro_crew.config.loader import KiroCrewConfig
                from kiro_crew.vector_memory import open_member_database

                config = KiroCrewConfig.load()
                store = open_member_database(
                    db_path,
                    member_id=config.memory_stores[store_name].owner_member_id,
                    store_id=store_name,
                )
            else:
                store = (
                    VectorMemoryStore() if db_path is None else VectorMemoryStore(db_path=db_path)
                )
        except Exception:
            logger.warning(
                "could not open memory store %r for an injection audit", store_name, exc_info=True
            )
            findings.append(_unauditable_finding(store_name))
            continue
        try:
            if not member_store:
                store.init()
            _scan_memory_store(store, store_name, findings)
        except Exception:
            logger.warning(
                "memory store %r could not be audited for injection patterns",
                store_name,
                exc_info=True,
            )
            findings.append(_unauditable_finding(store_name))
        finally:
            # In a finally so a raise anywhere above cannot leak this store's
            # sqlite connection, and so a many-store install does not accumulate
            # one open handle per store for the length of the scan.
            try:
                store.close()
            except Exception:
                logger.debug("closing memory store %r failed", store_name, exc_info=True)
    _scan_lessons_tier(findings)
    return findings


def _scan_lessons_tier(findings: list[dict]) -> None:
    """Append every store's lessons-file findings into *findings*.

    Its own function so the two ``scan_memory`` exits — the ordinary one and the early
    return taken when the optional vector stack will not import — cannot drift on
    whether this tier ran.

    TOTAL: a failure costs at most one store's lessons tier, never the caller.
    ``scan_memory`` is reached from two CLI verbs, and an exception here would replace an
    audit report with a traceback — including the vector findings already collected. The
    per-store backstop still REPORTS, so a store lost to an unexpected raise is
    "unknown", not "clean"; :func:`_scan_store_lessons` handles the read failures it can
    name itself.
    """
    try:
        targets = _lessons_files_to_scan()
    except Exception:
        logger.warning("no memory store's lessons tier could be enumerated", exc_info=True)
        return
    for store_name, lessons_path in targets:
        try:
            _scan_store_lessons(store_name, lessons_path, findings)
        except Exception:
            logger.warning(
                "memory store %r could not have its lessons tier audited",
                store_name,
                exc_info=True,
            )
            findings.append(_unauditable_lessons_finding(store_name, lessons_path))


def should_record_observe_history(
    channel_history: object | None,
    user_authorized: bool,
) -> bool:
    """Return True if an observe-mode message should be recorded.

    Only authorized users' messages are recorded to prevent non-owner
    prompt injection via shared channel traffic.
    """
    return channel_history is not None and user_authorized
