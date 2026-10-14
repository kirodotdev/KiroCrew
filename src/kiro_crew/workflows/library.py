"""Durable reusable workflow definitions with revisions and lineage."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, List, Optional

from kiro_crew import platform_compat
from kiro_crew.atomic_write import replace_with_retry
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.slugs import slug_hash_fallback
from kiro_crew.workflows.store import default_workflow_library_dir

logger = logging.getLogger(__name__)

_LIBRARY_SUBDIR = "library"
_SCHEMA_VERSION = 2
_MAX_SLUG_LENGTH = 64
_DEFAULT_SEARCH_LIMIT = 3
_MIN_PREFIX_MATCH_LENGTH = 3
SOURCE_FORMAT_PYTHON = "python"
SOURCE_FORMAT_TASK_PLAN = "task-plan"
SOURCE_FORMATS = frozenset({SOURCE_FORMAT_PYTHON, SOURCE_FORMAT_TASK_PLAN})
_WORD_RE = re.compile(r"[a-z0-9]+")
_IGNORED_SEARCH_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "for",
        "in",
        "of",
        "the",
        "to",
        "workflow",
        "with",
    }
)


class SensitiveWorkflowSourceError(ValueError):
    """Raised when persistence redaction would change executable source."""


class WorkflowSlugTakenError(ValueError):
    """Raised when a create that must keep its exact slug finds it already used."""


class WorkflowRefMovedError(LookupError):
    """Raised when a ref reaches a different workflow than the caller confirmed.

    A rename frees a slug at once, so a slug the dashboard captured can name
    another workflow by the time a request arrives; the caller sends the id it
    showed, and a different id is refused rather than written or removed.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        redacted, _ = redact_exfiltration_urls(value)
        redacted, _ = redact_credentials(redacted)
        return redacted
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _slugify(value: str) -> str:
    words = _WORD_RE.findall(value.lower())
    slug = "-".join(words)[:_MAX_SLUG_LENGTH].strip("-")
    return slug or slug_hash_fallback(value, "workflow")


def derived_slug(name: str) -> str:
    """The slug :meth:`WorkflowDefinitionLibrary.create` gives *name* when no slug is asked for.

    The same redaction and slugification the create applies, before any ``-2``
    suffix, so a caller can show the ``/workflow`` command a name saves under.
    """
    return _slugify(str(_redact(str(_redact(name)))))


def _source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _tokens(value: str) -> set[str]:
    return {word for word in _WORD_RE.findall(value.lower()) if word not in _IGNORED_SEARCH_WORDS}


def _token_matches(query: str, candidate: str) -> bool:
    # A prefix only counts when the shorter word is long enough to mean
    # something: a lone ``r`` in a script would otherwise match every query
    # word that starts with ``r``. Short words still match exactly.
    if min(len(query), len(candidate)) < _MIN_PREFIX_MATCH_LENGTH:
        return query == candidate
    return query == candidate or query.startswith(candidate) or candidate.startswith(query)


def _is_removed(definition: dict[str, Any]) -> bool:
    """A tombstoned definition: kept on disk for lineage, absent everywhere else."""
    return bool(definition.get("removed_at"))


def _refs(definition: dict[str, Any]) -> set[str]:
    """The refs a record answers to when nothing refuses them: its id and slug."""
    return {str(ref) for ref in (definition.get("id"), definition.get("slug")) if ref}


def _reserved_refs(definitions: Iterable[dict[str, Any]]) -> set[str]:
    """The id and slug each removed definition held when it was removed.

    Each is reserved for good, whether a later ref matches it as an id or as
    a slug: a ``/workflow <slug>`` written down before a removal must never run a
    workflow saved later under that name. Resolution refuses a ref in this
    set, whether a live record carries it as its id or as its slug
    (``_ref_index``), and slug allocation never hands one out (``_taken_refs``).
    The set holds what each tombstone records and nothing more: a slug a
    rename gave up is not in it, and a tombstone that does not read releases
    its refs (``_warn_unreadable``).
    """
    reserved: set[str] = set()
    for definition in definitions:
        if _is_removed(definition):
            reserved |= _refs(definition)
    return reserved


def _taken_refs(definitions: Iterable[dict[str, Any]], *, excluding_id: str = "") -> set[str]:
    """Every ref slug allocation must not hand out: the reserved ones, and the
    id and slug of every live record except the one being renamed."""
    definitions = list(definitions)
    taken = _reserved_refs(definitions)
    for definition in definitions:
        if not _is_removed(definition) and definition.get("id") != excluding_id:
            taken |= _refs(definition)
    return taken


def _ref_text(value: Any) -> str:
    """``value`` when it is a non-blank string, else ``""``: a missing,
    ``None``, non-string or blank ref names nothing."""
    return value if isinstance(value, str) and value.strip() else ""


def _ref_index(definitions: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The live definition each ref reaches on one read of the library.

    A ref a removed record holds reaches nothing, even when a hand-written
    live record carries it as its id or as its slug (``_reserved_refs``).
    Every other ref reaches the first live record, in the folder's file
    order, that has it as its id, else the first that has it as its slug: ids
    are matched before slugs, so a free id reaches its own record whatever
    order the folder lists files in. Built once per read, so what a read
    offers (``_presented``) and what a ref then runs (``_resolve``) are the
    same answer.
    """
    definitions = list(definitions)
    reserved = _reserved_refs(definitions)
    live = [definition for definition in definitions if not _is_removed(definition)]
    index: dict[str, dict[str, Any]] = {}
    for key in ("id", "slug"):
        for definition in live:
            ref = _ref_text(definition.get(key))
            if ref and ref not in reserved:
                index.setdefault(ref, definition)
    return index


def _resolve(workflow_ref: str, index: dict[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The live definition a ref reaches on the read ``index`` was built
    from, or ``None``: the one answer to which record a ref names.

    Lineage never comes through here: it names a parent by id and revision,
    removed parents included (``lineage_candidates``).
    """
    return index.get(workflow_ref) if isinstance(workflow_ref, str) else None


def _live_with_id(
    workflow_id: str, definitions: Iterable[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    """The live definition stored under ``workflow_id``.

    For a record a caller already resolved from a user's ref with ``get``: it
    finds that record again, so a ref refusal is applied once, at ``get``.
    """
    for definition in definitions:
        if not _is_removed(definition) and definition.get("id") == workflow_id:
            return definition
    return None


#: Computed on every read and never stored; a stored copy is ignored.
_READ_TIME_KEYS = ("shadowed_refs",)


def _presented(definition: dict[str, Any], index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """A live definition as reads return it. Never stored.

    ``shadowed_refs`` lists its own slug and id, in that order, when
    ``_resolve`` does not take them to a record with its id on this read: a
    removed record holds the ref, or another live record answers to it first
    (one whose id is this record's slug, or an earlier one with the same
    slug). It is set only when one is listed. Every surface offers a ref
    left out of it (``command_ref``), so an offered command runs this
    record; with both listed none does, and the library list keeps it,
    marked, until its file gives it a new id and slug or is deleted, while
    every other list leaves it out.
    """
    if any(key in definition for key in _READ_TIME_KEYS):
        definition = {key: value for key, value in definition.items() if key not in _READ_TIME_KEYS}
    own_id = definition.get("id")
    shadowed: list[str] = []
    for ref in (_ref_text(definition.get("slug")), _ref_text(own_id)):
        reached = _resolve(ref, index) if ref else None
        if ref and ref not in shadowed and (reached is None or reached.get("id") != own_id):
            shadowed.append(ref)
    return {**definition, "shadowed_refs": shadowed} if shadowed else definition


def command_ref(definition: dict[str, Any]) -> str:
    """The ref ``/workflow`` reaches a listed definition by: its slug, else its
    id, leaving out any its read listed in ``shadowed_refs``, or ``""`` when
    that leaves none, which no list but the library's own offers. Never
    ``None`` or a blank string."""
    shadowed = definition.get("shadowed_refs")
    if not isinstance(shadowed, list):
        shadowed = []
    for ref in (_ref_text(definition.get("slug")), _ref_text(definition.get("id"))):
        if ref and ref not in shadowed:
            return ref
    return ""


class WorkflowDefinitionLibrary:
    """JSON-per-definition library in the agent-protected Crew data home."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self._library_dir = (
            Path(base_dir) / _LIBRARY_SUBDIR
            if base_dir is not None
            else default_workflow_library_dir()
        )
        #: Each unreadable file already warned about, with the version warned.
        self._unreadable: dict[str, tuple[Any, ...]] = {}

    @property
    def library_dir(self) -> Path:
        return self._library_dir

    def _path_for(self, workflow_id: str) -> Path:
        safe = "".join(char for char in workflow_id if char.isalnum() or char in ("_", "-"))
        if safe != workflow_id:
            digest = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()[:12]
            safe = f"{safe}-{digest}" if safe else digest
        return self._library_dir / f"{safe}.json"

    def _load_entries(self) -> list[tuple[Path, dict[str, Any]]]:
        """Every readable definition paired with the file it was read from.

        A record's file name is not part of its identity: a hand-placed or
        copied record can sit under any ``*.json`` name, so a write that must
        land on the record a reader sees uses this path, not ``_path_for``.
        """
        if not self._library_dir.is_dir():
            return []
        entries: list[tuple[Path, dict[str, Any]]] = []
        for path in self._library_dir.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and value.get("id"):
                    # Version 1 predates multi-format definitions and every
                    # record it could contain was validated Python source.
                    value.setdefault("format", SOURCE_FORMAT_PYTHON)
                    entries.append((path, value))
                    # Read again, it is warned about afresh if it breaks again.
                    self._unreadable.pop(str(path), None)
                    continue
            except Exception:  # noqa: BLE001 - one corrupt definition must not hide the library
                logger.debug("workflow library: skip unreadable %s", path, exc_info=True)
            self._warn_unreadable(path)
        return entries

    def _warn_unreadable(self, path: Path) -> None:
        """Warn about a library file no read can use, once until it changes.

        A removed record's id and slug stay reserved only while its tombstone
        reads, so a damaged or hand-edited one releases them; the owner is told
        once per file and version, not on every read of the library.
        """
        try:
            stat = path.stat()
            version: tuple[Any, ...] = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            version = ()
        key = str(path)
        if self._unreadable.get(key) == version:
            return
        self._unreadable[key] = version
        logger.warning(
            "workflow library: %s is not a readable workflow record and is skipped; "
            "if it was a removed workflow, its id and slash-command name are free again",
            path,
        )

    def _load_all(self) -> list[dict[str, Any]]:
        return [definition for _, definition in self._load_entries()]

    def _live(self) -> list[dict[str, Any]]:
        """Definitions a user can list, search or run, as reads return them."""
        definitions = self._load_all()
        index = _ref_index(definitions)
        return [_presented(item, index) for item in definitions if not _is_removed(item)]

    @contextmanager
    def _revisions_lock(self) -> Iterator[None]:
        """Cross-process lock ordering revision updates and removals.

        Each service has its own in-process lock. The definition library is
        shared by gateway and adapted harness processes, so a read, its check
        (revision, slug, tombstone) and the replacement must share one
        cross-process lock, or a removal could land between an update's read
        and its write and be overwritten by a live record.
        """
        platform_compat.make_owner_only_dir(self._library_dir)
        lock_path = self._library_dir / ".revisions.lock"
        fd = platform_compat.open_create_or_existing(lock_path, os.O_RDWR, 0o600)
        try:
            with platform_compat.file_lock(fd, exclusive=True, wait=True):
                yield
        finally:
            os.close(fd)

    def _write(self, definition: dict[str, Any], path: Optional[Path] = None) -> None:
        """Gate new source, then store ``definition`` at ``path`` if named.

        A revision names the file its record was read from: under a hand-made
        name, or beside a tombstone of the same id at ``_path_for``, the record
        is updated where it is and no other file is touched.
        """
        sources = [definition.get("source")]
        sources.extend(
            revision.get("source")
            for revision in definition.get("revisions", [])
            if isinstance(revision, dict)
        )
        if any(isinstance(source, str) and _redact(source) != source for source in sources):
            raise SensitiveWorkflowSourceError(
                "workflow source contains sensitive data or an exfiltration URL"
            )
        self._replace(_redact(definition), path)

    def _replace(self, definition: dict[str, Any], path: Optional[Path] = None) -> None:
        """Atomically store ``definition`` exactly as given, at ``path`` if named.

        ``_write`` is the gated path for new source. A tombstone calls this
        directly: it adds only ``removed_at`` to a record already on disk, so
        re-running today's source gate over revisions saved under older rules
        would make exactly the stale workflow a user wants gone unremovable.
        """
        platform_compat.make_owner_only_dir(self._library_dir)
        if path is None:
            path = self._path_for(str(definition["id"]))
        temp_path = path.with_suffix(".json.tmp")
        payload = json.dumps(definition, ensure_ascii=False, indent=2, default=str)
        try:
            temp_path.write_text(payload, encoding="utf-8")
            platform_compat.restrict_to_owner(temp_path)
            replace_with_retry(temp_path, path)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def slug_taken(self, slug: str) -> bool:
        """Whether a create asking for exactly *slug* would be refused.

        Reads the same refs the allocator does, so a slug a removed record
        holds is taken here too, though ``get`` reaches nothing for it.
        """
        return _slugify(slug) in _taken_refs(self._load_all())

    def _unique_slug(self, requested: str, *, excluding_id: str = "") -> str:
        base = _slugify(requested)
        # Every id and slug on disk, live or removed, is taken; only the
        # renamed record's own refs stay free for it.
        used = _taken_refs(self._load_all(), excluding_id=excluding_id)
        if base not in used:
            return base
        suffix = 2
        candidate = ""
        while True:
            suffix_text = f"-{suffix}"
            candidate = f"{base[: _MAX_SLUG_LENGTH - len(suffix_text)].rstrip('-')}{suffix_text}"
            if candidate not in used:
                break
            suffix += 1
        return candidate

    def create(
        self,
        *,
        source: str,
        name: str,
        description: str = "",
        slug: str = "",
        derived_from: Optional[dict[str, Any]] = None,
        source_format: str = SOURCE_FORMAT_PYTHON,
        exact_slug: bool = False,
    ) -> dict[str, Any]:
        """Create a definition with its own identity, even for identical source.

        A used slug normally gets a ``-2`` style suffix. With *exact_slug* the
        create raises :class:`WorkflowSlugTakenError` instead, so a caller that
        already showed ``/workflow <slug>`` never saves under another command.
        The slug check and the write share the cross-process revisions lock.
        """
        platform_compat.make_owner_only_dir(self._library_dir)
        lock_path = self._library_dir / ".revisions.lock"
        fd = platform_compat.open_create_or_existing(lock_path, os.O_RDWR, 0o600)
        try:
            with platform_compat.file_lock(fd, exclusive=True, wait=True):
                return self._create_unlocked(
                    source=source,
                    name=name,
                    description=description,
                    slug=slug,
                    derived_from=derived_from,
                    source_format=source_format,
                    exact_slug=exact_slug,
                )
        finally:
            os.close(fd)

    def _create_unlocked(
        self,
        *,
        source: str,
        name: str,
        description: str,
        slug: str,
        derived_from: Optional[dict[str, Any]],
        source_format: str,
        exact_slug: bool,
    ) -> dict[str, Any]:
        if source_format not in SOURCE_FORMATS:
            raise ValueError(f"unsupported workflow source format: {source_format}")
        content_hash = _source_hash(source)
        created_at = _now()
        workflow_id = f"wfd_{uuid.uuid4().hex[:16]}"
        safe_name = str(_redact(name))
        safe_description = str(_redact(description))
        # Credential scanners intentionally recognize their canonical casing.
        # Redact before slugification lowercases the input, or a real secret in
        # a name/explicit slug could be transformed into an unrecognized value.
        safe_slug_source = str(_redact(slug or safe_name))
        unique_slug = self._unique_slug(safe_slug_source)
        if exact_slug and unique_slug != _slugify(safe_slug_source):
            raise WorkflowSlugTakenError(f"'{_slugify(safe_slug_source)}' is already a workflow")
        definition: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "id": workflow_id,
            "slug": unique_slug,
            "name": safe_name,
            "description": safe_description,
            "format": source_format,
            "created_at": created_at,
            "updated_at": created_at,
            "revision": 1,
            "source": source,
            "content_hash": content_hash,
            "derived_from": derived_from,
            "revisions": [{"revision": 1, "source": source, "created_at": created_at}],
        }
        self._write(definition)
        return definition

    def update(
        self,
        workflow_id: str,
        *,
        source: str,
        expected_revision: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        slug: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Append a revision to the live definition with this id, or return
        ``None`` on missing/stale/removed definitions.

        Callers resolve a user's ref with ``get`` first and pass the id it
        returned, so a reserved ref is refused there, once.
        """
        with self._revisions_lock():
            return self._update_unlocked(
                workflow_id,
                source=source,
                expected_revision=expected_revision,
                name=name,
                description=description,
                slug=slug,
            )

    def remove(
        self, workflow_ref: str, *, expected_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Tombstone a definition, or return ``None`` when no live one matches.

        With ``expected_id``, the ref must still resolve to that id: one that
        now reaches a different workflow raises ``WorkflowRefMovedError`` and
        nothing is tombstoned. The check runs under the revisions lock, with
        the resolution it checks.

        The record and every revision stay on disk with ``removed_at`` set.
        Who removed it is recorded by the route's SEL audit row, not here.
        From then on it is absent from ``get``, ``list`` and ``search``, and
        only ``lineage_candidates`` still returns it, so drafts and runs
        adapted from it keep saving. Its id and slug stay reserved, so a
        ``/workflow <slug>`` saved elsewhere never reaches a different workflow.

        Each tombstone replaces the file its record was read from. When more
        than one file holds a live copy of the id, every copy is tombstoned,
        so no copy stays runnable after a removal reports success. A failed
        write raises. Before any copy is tombstoned a retry removes the
        record; after one is, its id is reserved, so a copy still live answers
        only to a slug of its own that no removed record holds, and with none
        is reached by no ref and lists as having no slash command. The
        dashboard re-reads the list on a not-found answer, so such a copy is
        never reported as removed.

        A ref resolves as in ``get``. When two hand-made records with
        different ids share one slug, removing by that slug removes the one
        ``get`` reaches, and from then on the slug answers not found for both:
        the other record stays live and answers only to its id. The dashboard
        removes by id, or by slug when a removed record holds the id.
        """
        with self._revisions_lock():
            entries = self._load_entries()
            definitions = [definition for _, definition in entries]
            current = _resolve(workflow_ref, _ref_index(definitions))
            if current is None:
                return None
            if expected_id is not None and current.get("id") != expected_id:
                raise WorkflowRefMovedError(workflow_ref)
            removed_at = _now()
            for path, definition in entries:
                if not _is_removed(definition) and definition.get("id") == current["id"]:
                    self._replace({**definition, "removed_at": removed_at}, path)
            return {**current, "removed_at": removed_at}

    def _update_unlocked(
        self,
        workflow_id: str,
        *,
        source: str,
        expected_revision: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        slug: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        # The stored record, not ``get``'s presented copy: the read-time flags
        # must never be written back. The caller resolved the user's ref with
        # ``get``; this finds that record again by its id under the lock, and
        # the revision goes back to the file it was read from, as a tombstone
        # does: ``_path_for`` may hold this id's tombstone, which must stay.
        entries = self._load_entries()
        definitions = [definition for _, definition in entries]
        current = _live_with_id(workflow_id, definitions)
        if current is None or current.get("revision") != expected_revision:
            return None
        path = next(path for path, definition in entries if definition is current)

        updated_at = _now()
        revision = expected_revision + 1
        revisions = list(current.get("revisions", []))
        revisions.append({"revision": revision, "source": source, "created_at": updated_at})
        safe_name = None if name is None else str(_redact(name))
        safe_description = None if description is None else str(_redact(description))
        safe_slug = None if slug is None or not slug.strip() else str(_redact(slug))
        updated = {
            **current,
            "name": current.get("name", "") if safe_name is None else safe_name,
            "description": (
                current.get("description", "") if safe_description is None else safe_description
            ),
            "slug": (
                current.get("slug", "")
                if safe_slug is None
                else self._unique_slug(safe_slug, excluding_id=str(current["id"]))
            ),
            "updated_at": updated_at,
            "revision": revision,
            "source": source,
            "content_hash": _source_hash(source),
            "revisions": revisions,
        }
        self._write(updated, path)
        # Offered refs come from the library as this write leaves it: a new
        # slug reaches the record, and an unchanged one reaches it as before.
        after = [updated if definition is current else definition for definition in definitions]
        return _presented(updated, _ref_index(after))

    def get(self, workflow_ref: str) -> Optional[dict[str, Any]]:
        """Resolve a live definition by id or slug; a ref a removed record holds answers ``None``."""
        index = _ref_index(self._load_all())
        definition = _resolve(workflow_ref, index)
        return None if definition is None else _presented(definition, index)

    def list(self) -> list[dict[str, Any]]:
        """List live definitions with most recently updated first."""
        return sorted(self._live(), key=lambda item: str(item.get("updated_at", "")), reverse=True)

    def has_live(self, workflow_id: str) -> bool:
        """Whether a live record is stored under ``workflow_id``.

        An identity check on a record's own id, not a user's ref, so no ref
        refusal applies.
        """
        return _live_with_id(workflow_id, self._load_all()) is not None

    def lineage_candidates(self) -> List[dict[str, Any]]:
        """Every definition a ``derived_from`` reference may name, removed ones included.

        This is the only read that returns tombstoned definitions. Lineage
        names a parent by id and revision, never by slug, so a removed
        parent still proves where a draft or run came from.
        """
        return self._load_all()

    def search(
        self,
        intent: str,
        limit: int = _DEFAULT_SEARCH_LIMIT,
        *,
        source_format: str = "",
    ) -> List[dict[str, Any]]:
        """Rank saved definitions using deterministic local lexical similarity."""
        query_tokens = _tokens(intent)
        if not query_tokens or limit <= 0:
            return []

        ranked: list[tuple[int, str, dict[str, Any]]] = []
        for definition in self._live():
            if source_format and definition.get("format") != source_format:
                continue
            title_tokens = _tokens(f"{definition.get('slug', '')} {definition.get('name', '')}")
            description_tokens = _tokens(str(definition.get("description", "")))
            source_tokens = _tokens(str(definition.get("source", "")))
            score = 0
            for query in query_tokens:
                score += 8 * sum(_token_matches(query, token) for token in title_tokens)
                score += 4 * sum(_token_matches(query, token) for token in description_tokens)
                score += sum(_token_matches(query, token) for token in source_tokens)
            if score:
                ranked.append((score, str(definition.get("updated_at", "")), definition))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [definition for _, _, definition in ranked[:limit]]
