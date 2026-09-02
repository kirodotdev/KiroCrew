"""Durable reusable workflow definitions with revisions and lineage."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from kiro_crew import platform_compat
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.workflows.store import default_workflow_library_dir

logger = logging.getLogger(__name__)

_LIBRARY_SUBDIR = "library"
_SCHEMA_VERSION = 2
_MAX_SLUG_LENGTH = 64
_DEFAULT_SEARCH_LIMIT = 3
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
    return slug or "workflow"


def _source_hash(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _tokens(value: str) -> set[str]:
    return {word for word in _WORD_RE.findall(value.lower()) if word not in _IGNORED_SEARCH_WORDS}


def _token_matches(query: str, candidate: str) -> bool:
    return query == candidate or query.startswith(candidate) or candidate.startswith(query)


class WorkflowDefinitionLibrary:
    """JSON-per-definition library in the agent-protected Crew data home."""

    def __init__(self, base_dir: Optional[Path] = None) -> None:
        self._library_dir = (
            Path(base_dir) / _LIBRARY_SUBDIR
            if base_dir is not None
            else default_workflow_library_dir()
        )

    @property
    def library_dir(self) -> Path:
        return self._library_dir

    def _path_for(self, workflow_id: str) -> Path:
        safe = "".join(char for char in workflow_id if char.isalnum() or char in ("_", "-"))
        if safe != workflow_id:
            digest = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()[:12]
            safe = f"{safe}-{digest}" if safe else digest
        return self._library_dir / f"{safe}.json"

    def _load_all(self) -> list[dict[str, Any]]:
        if not self._library_dir.is_dir():
            return []
        definitions: list[dict[str, Any]] = []
        for path in self._library_dir.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict) and value.get("id"):
                    # Version 1 predates multi-format definitions and every
                    # record it could contain was validated Python source.
                    value.setdefault("format", SOURCE_FORMAT_PYTHON)
                    definitions.append(value)
            except Exception:  # noqa: BLE001 - one corrupt definition must not hide the library
                logger.debug("workflow library: skip unreadable %s", path, exc_info=True)
        return definitions

    def _write(self, definition: dict[str, Any]) -> None:
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
        platform_compat.make_owner_only_dir(self._library_dir)
        path = self._path_for(str(definition["id"]))
        temp_path = path.with_suffix(".json.tmp")
        payload = json.dumps(_redact(definition), ensure_ascii=False, indent=2, default=str)
        try:
            temp_path.write_text(payload, encoding="utf-8")
            platform_compat.restrict_to_owner(temp_path)
            os.replace(temp_path, path)
        except Exception:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _unique_slug(self, requested: str, *, excluding_id: str = "") -> str:
        base = _slugify(requested)
        used = {
            str(item.get("slug", "")) for item in self._load_all() if item.get("id") != excluding_id
        }
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
    ) -> dict[str, Any]:
        """Create a definition with its own identity, even for identical source."""
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
        definition: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "id": workflow_id,
            "slug": self._unique_slug(safe_slug_source),
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
        """Append a revision, or return ``None`` on missing/stale definitions."""
        current = self.get(workflow_id)
        if current is None or current.get("revision") != expected_revision:
            return None

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
        self._write(updated)
        return updated

    def get(self, workflow_ref: str) -> Optional[dict[str, Any]]:
        """Resolve a definition by stable id or unique slash-command slug."""
        for definition in self._load_all():
            if definition.get("id") == workflow_ref or definition.get("slug") == workflow_ref:
                return definition
        return None

    def list(self) -> list[dict[str, Any]]:
        """List definitions with most recently updated first."""
        return sorted(
            self._load_all(), key=lambda item: str(item.get("updated_at", "")), reverse=True
        )

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
        for definition in self._load_all():
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
