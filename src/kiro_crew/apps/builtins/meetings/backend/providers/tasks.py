"""Task-provider seam — where a reviewed meeting action item gets filed.

Upstream wired this app directly to one company-internal task system: the
destination editor, the per-task override, the preset schema, and the
create-task agent prompt all named it, and there was no way to point the app
anywhere else.

This module replaces that with an extension point in the same shape as
``kiro_crew.embeddings``' ``EmbeddingBackend`` / ``register_embedding_backend``:
an ABC (:class:`TaskProvider`), a name-keyed factory registry
(:func:`register_task_provider`), and a resolver (:func:`get_task_provider`).

**Exactly one implementation ships here** — :class:`LocalTaskProvider`, which
writes to a KiroCrew-local, app-scoped task ledger. That is deliberate: the
seam exists so an out-of-repo companion can register a provider for whatever
tracker an organization actually uses, and the public app is complete without
one. Nothing in the app branches on a provider name.

Why a local JSON ledger rather than ``task_models.Project``: the task runner's
``Project``/``Task`` dataclasses model an *autonomous execution plan* — an
ordered, dependency-linked list of steps a single agent works through, with
attempt counts and a state machine that drives the runner. A meeting action item
is a different thing: a durable, human-owned to-do with an assignee and a
priority that nobody executes automatically. Reusing ``Project`` would mean
inventing a fake spec for every meeting and leaving the executor fields
permanently unused, so the ledger keeps its own small record and the shape stays
honest.
"""

from __future__ import annotations

import abc
import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.atomic_write import atomic_write
from kiro_crew.security import redact
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.meetings")

_LEDGER_FILE = "task-ledger.json"
_MAX_LEDGER_ENTRIES = 2000

#: Serializes the ledger read-append-write. Module level, not per instance:
#: a provider is constructed per request, so an instance lock would guard
#: nothing. Held only around local file IO, never across an await.
_LEDGER_LOCK = threading.Lock()
_MAX_FIELD_LEN = 2000


@dataclass
class TaskDraft:
    """A reviewed action item, ready to be filed."""

    description: str
    meeting_id: str = ""
    meeting_title: str = ""
    assignee: str = ""
    priority: str = k.DEFAULT_TASK_PRIORITY
    context: str = ""
    labels: list[str] = field(default_factory=list)

    def sanitized(self) -> "TaskDraft":
        """A copy with every LLM/user-derived string redacted and length-capped.

        Transcripts and extracted tasks are LLM output, and a filed task is an
        external surface, so credential + exfiltration-URL redaction runs before
        anything leaves the process (AUTOSDE ``backend-security-controls``).
        """
        def clean(value: str) -> str:
            return redact(str(value or "").strip())[:_MAX_FIELD_LEN]

        priority = self.priority if self.priority in k.TASK_PRIORITIES else k.DEFAULT_TASK_PRIORITY
        return TaskDraft(
            description=clean(self.description),
            meeting_id=clean(self.meeting_id),
            meeting_title=clean(self.meeting_title),
            assignee=clean(self.assignee),
            priority=priority,
            context=clean(self.context),
            labels=[clean(lab) for lab in self.labels if isinstance(lab, str) and lab.strip()][:20],
        )


@dataclass
class TaskRef:
    """The provider's receipt for a filed task."""

    provider: str
    id: str
    url: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskProvider(abc.ABC):
    """Abstract destination for a filed meeting action item.

    Contract:

    * :meth:`create` receives an ALREADY-SANITIZED :class:`TaskDraft` (the
      caller runs :meth:`TaskDraft.sanitized`) and returns a :class:`TaskRef`.
    * It must raise on failure rather than return a fake ref — the review UI
      shows "not filed" so the user can retry.
    * :attr:`provider_id` is the value users put in ``config.json``'s
      ``task_provider`` field.
    * Implementations must be safe to call from a worker thread; the resolver
      does not serialize calls.
    """

    @property
    @abc.abstractmethod
    def provider_id(self) -> str:
        """Stable identifier, e.g. ``"local"``."""

    @property
    @abc.abstractmethod
    def display_name(self) -> str:
        """Human-readable label for the settings UI."""

    @abc.abstractmethod
    def create(self, draft: TaskDraft) -> TaskRef:
        """File *draft* and return its reference. Raises on failure."""

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recently filed tasks, newest first. Optional — default is empty."""
        return []


class LocalTaskProvider(TaskProvider):
    """The shipped provider: an app-scoped JSON ledger under the data dir.

    ``root`` mirrors ``store``'s convention so tests point at a tmp dir.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root

    @property
    def provider_id(self) -> str:
        return k.TASK_PROVIDER_LOCAL

    @property
    def display_name(self) -> str:
        return "KiroCrew tasks (local)"

    def _path(self) -> Path:
        return store.data_dir(self._root) / _LEDGER_FILE

    def _read(self) -> list[dict[str, Any]]:
        path = self._path()
        if not path.is_file():
            return []
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("meetings: unreadable task ledger at %s", path)
            return []
        entries = doc.get("tasks") if isinstance(doc, dict) else None
        return entries if isinstance(entries, list) else []

    def create(self, draft: TaskDraft) -> TaskRef:
        # The whole read-append-write is under one lock. `create` runs on the
        # subprocess executor (see `handle_file_task`), so two filings genuinely
        # execute in parallel: both would read the same list, both would append one
        # task, and the second `atomic_write` would land a snapshot missing the
        # first — the write is atomic, the read-modify-write was not. Both requests
        # still report success, so the loss is silent.
        #
        # A module-level lock rather than a per-instance one: `get_task_provider`
        # constructs a fresh provider per request, so an instance attribute would
        # guard nothing.
        with _LEDGER_LOCK:
            return self._create_locked(draft)

    def _create_locked(self, draft: TaskDraft) -> TaskRef:
        """Append one task to the ledger. Caller MUST hold :data:`_LEDGER_LOCK`."""
        entries = self._read()
        ref = TaskRef(
            provider=self.provider_id,
            id=f"mt-{uuid.uuid4().hex[:12]}",
            created_at=store.utc_now_iso(),
        )
        entries.append(
            {
                **asdict(draft),
                "id": ref.id,
                "state": "open",
                "created_at": ref.created_at,
                "created_ts": time.time(),
            }
        )
        # Newest-last list, oldest trimmed: the ledger is a durable record, not
        # a queue, so an unbounded file is the only real failure mode.
        entries = entries[-_MAX_LEDGER_ENTRIES:]
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(
            path, json.dumps({"tasks": entries, "updated_at": ref.created_at}, indent=2)
        )
        sel().log_api_access(
            caller=f"app:{k.APP_NAME}",
            operation="meetings.task_create",
            outcome="ok",
            resources=f"{self.provider_id}:{ref.id}",
        )
        return ref

    def list_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        return list(reversed(self._read()))[:bounded]


# ── registry ────────────────────────────────────────────────────────────────

TaskProviderFactory = Callable[[], TaskProvider]

_factories: dict[str, TaskProviderFactory] = {
    k.TASK_PROVIDER_LOCAL: LocalTaskProvider,
}


def register_task_provider(provider_id: str, factory: TaskProviderFactory | None) -> None:
    """Register (or, with ``None``, unregister) a task provider.

    The seam an out-of-repo companion uses to add its own tracker. Passing a
    factory for an existing id REPLACES it, so an edition may also override the
    shipped local provider. Unknown ids simply never resolve, and
    :func:`get_task_provider` falls back to the local provider — a config typo
    degrades to "filed locally", never to a lost task.
    """
    key = (provider_id or "").strip().lower()
    if not key:
        raise ValueError("provider_id must be a non-empty string")
    if factory is None:
        _factories.pop(key, None)
        return
    _factories[key] = factory


def available_task_providers() -> list[dict[str, str]]:
    """Registered providers as ``{"id", "label"}`` rows for the settings UI."""
    rows: list[dict[str, str]] = []
    for key, factory in sorted(_factories.items()):
        try:
            rows.append({"id": key, "label": factory().display_name})
        except Exception:  # pragma: no cover — a broken edition factory
            logger.warning("meetings: task provider %s failed to construct", key, exc_info=True)
    return rows


def get_task_provider(provider_id: str = "", root: Path | None = None) -> TaskProvider:
    """Resolve *provider_id*, falling back to the shipped local provider.

    ``root`` is threaded into the local provider only (it is the sole
    filesystem-backed implementation); a registered edition factory takes no
    arguments, matching ``register_embedding_backend``'s zero-arg factory.
    """
    key = (provider_id or k.DEFAULT_TASK_PROVIDER).strip().lower()
    factory = _factories.get(key)
    if factory is None:
        logger.info("meetings: unknown task provider %r — using local", key)
        return LocalTaskProvider(root)
    if factory is LocalTaskProvider:
        return LocalTaskProvider(root)
    return factory()
