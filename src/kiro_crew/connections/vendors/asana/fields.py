"""Asana task/project field model and input/output normalization (pure logic).

WHAT THIS OWNS
==============
The typed shape of an Asana task and project as this connector models them,
and the normalization rules that turn loose caller intent (and a raw Asana
JSON object) into that shape. Network-free and credential-free: nothing here
makes a call or holds a token.

FOUR VENDOR CONSTRAINTS ENFORCED HERE
=====================================
1. **A GID is an opaque string, never a number.** Asana addresses every
   resource by GID -- a globally-unique identifier that HAPPENS to be spelled
   with decimal digits today but is documented as an opaque string. Parsing it
   as an int (or doing any arithmetic, ordering, or zero-padding on it) is a
   latent bug the day Asana widens the alphabet. :func:`normalize_gid` accepts
   the string, rejects the empty/whitespace/None case and anything carrying a
   separator, and returns it verbatim -- it never coerces to ``int``. The one
   special value is the literal ``"me"``, accepted only where Asana documents
   it (user endpoints), modeled by :data:`ME_SENTINEL` and NOT treated as a
   general GID substitute.

2. **``due_on`` and ``due_at`` are two different fields.** ``due_on`` is a
   date-ONLY value (``YYYY-MM-DD``, no time, no zone); ``due_at`` is a
   date-AND-TIME value (ISO 8601 datetime, ``...Z``). They are documented as
   distinct, mutually-relevant task fields -- NOT two encodings of one concept.
   This module refuses to (a) carry both at once, (b) infer one from the other,
   or (c) silently promote a date to a datetime or truncate a datetime to a
   date. A caller either supplies a ``due_on`` or a ``due_at`` or neither; any
   attempt to set both is a :class:`AsanaFieldError`. The same rule holds for
   ``start_on`` / ``start_at``.

3. **A task can belong to MANY projects.** ``projects`` is a set of project
   GIDs, not a single one; project membership is many-to-many. The model never
   collapses it to one project, and de-dups while preserving that a task with
   zero declared projects (workspace-rooted) is a legal, distinct state from a
   task in one project.

4. **Workspace membership is carried explicitly, never implied.** A task's
   workspace is not inferred from its projects (a project resolves to a
   workspace, but the connector does not silently derive one from the other).
   When a task is created workspace-rooted (no project), the workspace GID is
   REQUIRED and explicit. :func:`normalize_task_create` refuses a create that
   names neither a workspace nor at least one project.

WHAT THIS DELIBERATELY DOES NOT OWN
===================================
No vendor-error taxonomy (that is :mod:`kiro_crew.connections.vendors.asana.errors`),
no pagination (that is :mod:`kiro_crew.connections.vendors.asana.pagination`), no live
call. A structurally invalid FIELD shape raises :class:`AsanaFieldError`, a
plain ``ValueError`` subclass -- it is a shaping fault, not a mapped vendor
response.

The ``completed_at``-cleared-on-reopen behavior is an evidence UNKNOWN: this
module carries ``completed`` and ``completed_at`` as independent fields and
does NOT assert that reopening (``completed=false``) clears ``completed_at``,
because the evidence pass could not confirm it. A reader is given the raw
value, not a fabricated invariant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

#: The one non-GID token Asana accepts where a user id is expected. Valid ONLY
#: on user-scoped surfaces (get_user / GET /users/{id}); never a general GID.
ME_SENTINEL = "me"

# A date-only field: exactly YYYY-MM-DD, no time component. Matched by shape,
# not parsed into a date object -- calendar validity (e.g. month 13) is Asana's
# to reject; this layer guards only that a datetime was not passed where a date
# was meant.
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# A datetime carries a time separator. Asana emits full ISO 8601 with a 'T' and
# a zone/offset; the presence of 'T' (or a space-separated time) is what tells a
# datetime from a date, which is the exact confusion this module exists to stop.
_HAS_TIME = re.compile(r"[T ]\d{2}:")


class AsanaFieldError(ValueError):
    """A task/project field shape was invalid.

    A shaping fault only (a GID that is not a string, both ``due_on`` and
    ``due_at`` set, a datetime supplied for a date-only field, a create naming
    neither workspace nor project). Vendor errors are classified by
    :mod:`kiro_crew.connections.vendors.asana.errors`, never here.
    """


def normalize_gid(value: object, *, what: str = "gid", allow_me: bool = False) -> str:
    """Return ``value`` as an opaque GID string, or raise.

    A GID is treated as an opaque token: it is stripped of surrounding
    whitespace and returned VERBATIM. It is never parsed as an int, ordered,
    zero-padded, or otherwise arithmetically manipulated -- Asana documents the
    id as opaque and its digit-only spelling is not a contract.

    Rejects: a non-string, an empty/whitespace value, and a value carrying a
    ``/`` (which would silently inject path structure a caller did not model).
    ``allow_me`` permits the literal :data:`ME_SENTINEL` on user-scoped
    surfaces; it is refused everywhere else so ``"me"`` cannot leak into a
    project/task id position.
    """

    if not isinstance(value, str):
        raise AsanaFieldError(f"{what} must be a string GID, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        raise AsanaFieldError(f"{what} must be a non-empty GID")
    if stripped == ME_SENTINEL:
        if allow_me:
            return ME_SENTINEL
        raise AsanaFieldError(f"{what} must be a GID, not the 'me' sentinel here")
    if "/" in stripped:
        raise AsanaFieldError(f"{what} must not contain a path separator: {value!r}")
    return stripped


def normalize_gid_set(values: object, *, what: str = "gid") -> frozenset[str]:
    """Normalize an iterable of GIDs into a de-duplicated frozenset.

    Order is not significant for a membership set (a task's projects), so the
    result is a frozenset. An empty input yields an empty set -- a legal,
    distinct state (a task in no project is workspace-rooted, see
    :func:`normalize_task_create`), never an error. A ``str`` is rejected
    rather than silently iterated character-by-character.
    """

    if isinstance(values, str):
        raise AsanaFieldError(f"{what} set must be an iterable of GIDs, not a bare string")
    if not isinstance(values, Iterable):
        raise AsanaFieldError(f"{what} set must be iterable")
    items = list(values)
    return frozenset(normalize_gid(v, what=what) for v in items)


def _validate_date_only(value: Optional[str], field_name: str) -> Optional[str]:
    """Return a validated date-only ``YYYY-MM-DD`` string, or None.

    Refuses a datetime (a value carrying a time component) supplied where a
    date was meant -- the silent ``due_at`` -> ``due_on`` truncation this module
    exists to prevent.
    """

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AsanaFieldError(f"{field_name} must be a non-empty YYYY-MM-DD string")
    text = value.strip()
    if _HAS_TIME.search(text):
        raise AsanaFieldError(
            f"{field_name} is a date-only field but a datetime was supplied "
            f"({text!r}); use the *_at field for a specific time"
        )
    if not _DATE_ONLY.match(text):
        raise AsanaFieldError(f"{field_name} must be YYYY-MM-DD, got {text!r}")
    return text


def _validate_datetime(value: Optional[str], field_name: str) -> Optional[str]:
    """Return a validated datetime string, or None.

    Refuses a date-only value supplied where a datetime was meant -- the silent
    ``due_on`` -> ``due_at`` promotion this module exists to prevent. The zone
    and fractional-second shape is passed through verbatim (the caller owns
    formatting); this guards only that a TIME is present.
    """

    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AsanaFieldError(f"{field_name} must be a non-empty ISO 8601 datetime string")
    text = value.strip()
    if _DATE_ONLY.match(text) or not _HAS_TIME.search(text):
        raise AsanaFieldError(
            f"{field_name} is a datetime field but a date-only value was supplied "
            f"({text!r}); use the *_on field for a day with no specific time"
        )
    return text


def _reject_both(on_value: Optional[str], at_value: Optional[str], stem: str) -> None:
    """Refuse the case where both the date-only and datetime variant are set.

    ``due_on``/``due_at`` (and ``start_on``/``start_at``) are mutually
    exclusive by Asana's model; carrying both is ambiguous and is refused
    rather than one silently winning.
    """

    if on_value is not None and at_value is not None:
        raise AsanaFieldError(
            f"{stem}_on (date-only) and {stem}_at (datetime) are mutually exclusive; "
            "set at most one -- they are distinct fields, not interchangeable"
        )


@dataclass(frozen=True)
class DueDate:
    """A task's due value: EITHER a date-only ``on`` OR a datetime ``at``.

    Never both (refused at construction). ``both_unset`` distinguishes "no due
    date" from a set one. The two fields are never inter-derived: reading
    ``on`` when only ``at`` is set returns ``None``, not a truncated date.
    """

    on: Optional[str] = None
    at: Optional[str] = None

    def __post_init__(self) -> None:
        validated_on = _validate_date_only(self.on, "due_on")
        validated_at = _validate_datetime(self.at, "due_at")
        _reject_both(validated_on, validated_at, "due")
        object.__setattr__(self, "on", validated_on)
        object.__setattr__(self, "at", validated_at)

    @classmethod
    def from_read(cls, on: Optional[str], at: Optional[str]) -> "DueDate":
        """Build a ``DueDate`` from a READ (an Asana response), not a write.

        Asana populates BOTH ``due_on`` and ``due_at`` on a response whenever a
        due TIME is set (``due_on`` carries the date component of ``due_at``).
        That is a valid server shape, not the caller ambiguity ``_reject_both``
        guards against, so on the read path the datetime wins and the derived
        ``due_on`` is dropped -- preserving the no-inference invariant (``on``
        stays ``None`` when ``at`` is set) without rejecting a legal response.
        The strict mutual-exclusion check remains only for create/write intent.
        """

        if _validate_datetime(at, "due_at") is not None:
            return cls(on=None, at=at)
        return cls(on=on, at=at)

    @property
    def both_unset(self) -> bool:
        return self.on is None and self.at is None


@dataclass(frozen=True)
class StartDate:
    """A task's start value: EITHER a date-only ``on`` OR a datetime ``at``.

    Same mutual-exclusion and no-inference rules as :class:`DueDate`.
    """

    on: Optional[str] = None
    at: Optional[str] = None

    def __post_init__(self) -> None:
        validated_on = _validate_date_only(self.on, "start_on")
        validated_at = _validate_datetime(self.at, "start_at")
        _reject_both(validated_on, validated_at, "start")
        object.__setattr__(self, "on", validated_on)
        object.__setattr__(self, "at", validated_at)

    @classmethod
    def from_read(cls, on: Optional[str], at: Optional[str]) -> "StartDate":
        """Build a ``StartDate`` from a READ (an Asana response), not a write.

        Same read-path rule as :meth:`DueDate.from_read`: Asana returns
        ``start_on`` populated with the date component whenever ``start_at`` is
        set, which is a legal response shape; the datetime wins and the derived
        ``start_on`` is dropped rather than tripping ``_reject_both``. The strict
        mutual-exclusion check remains only for create/write intent.
        """

        if _validate_datetime(at, "start_at") is not None:
            return cls(on=None, at=at)
        return cls(on=on, at=at)

    @property
    def both_unset(self) -> bool:
        return self.on is None and self.at is None


@dataclass(frozen=True)
class TaskCreate:
    """A normalized task-create intent.

    ``workspace`` and ``projects`` together carry the destination: a task is
    workspace-rooted (``workspace`` set, ``projects`` empty), project-scoped
    (one or more ``projects``), or both. At least one destination is required
    -- enforced by :func:`normalize_task_create`, not assumed. ``workspace`` is
    NEVER inferred from ``projects``; if the caller wants both, both are stated.
    """

    name: str
    workspace: Optional[str]
    projects: frozenset[str]
    due: DueDate
    start: StartDate
    assignee: Optional[str]
    parent: Optional[str]
    notes: Optional[str]


def normalize_task_create(
    *,
    name: object,
    workspace: object = None,
    projects: object = (),
    due_on: Optional[str] = None,
    due_at: Optional[str] = None,
    start_on: Optional[str] = None,
    start_at: Optional[str] = None,
    assignee: object = None,
    parent: object = None,
    notes: object = None,
) -> TaskCreate:
    """Normalize loose create intent into a validated :class:`TaskCreate`.

    Enforces every field rule this module owns: a non-empty name; a GID-opaque
    workspace and project set; the due/start ``on``/``at`` mutual exclusion; and
    the destination rule -- a create naming NEITHER a workspace NOR at least one
    project is refused (Asana cannot place such a task, and inferring a
    workspace would violate the explicit-workspace constraint). ``assignee``
    accepts the ``me`` sentinel (it is a user position); ``parent`` does not
    (it is a task GID).
    """

    if not isinstance(name, str) or not name.strip():
        raise AsanaFieldError("task name must be a non-empty string")
    workspace_gid = None if workspace is None else normalize_gid(workspace, what="workspace")
    project_gids = normalize_gid_set(projects, what="project")
    if workspace_gid is None and not project_gids:
        raise AsanaFieldError(
            "a task create must name a workspace or at least one project; "
            "workspace is never inferred from projects"
        )
    return TaskCreate(
        name=name.strip(),
        workspace=workspace_gid,
        projects=project_gids,
        due=DueDate(on=due_on, at=due_at),
        start=StartDate(on=start_on, at=start_at),
        assignee=(
            None if assignee is None else normalize_gid(assignee, what="assignee", allow_me=True)
        ),
        parent=None if parent is None else normalize_gid(parent, what="parent"),
        notes=None if notes is None else str(notes),
    )


@dataclass(frozen=True)
class Task:
    """A normalized read of an Asana task object.

    ``projects`` is the many-to-many membership set. ``due``/``start`` preserve
    the on/at distinction. ``completed`` and ``completed_at`` are INDEPENDENT:
    this model does not assert ``completed_at`` is cleared on reopen (an
    evidence unknown), so a caller reads the raw value Asana returned rather
    than an inferred one.
    """

    gid: str
    name: str
    workspace: Optional[str]
    projects: frozenset[str]
    due: DueDate
    start: StartDate
    assignee: Optional[str]
    parent: Optional[str]
    completed: bool
    completed_at: Optional[str]


def parse_task(obj: Mapping[str, Any]) -> Task:
    """Read a raw Asana task JSON object into a :class:`Task`.

    Extracts membership (``projects[].gid``) into the many-to-many set, keeps
    ``due_on``/``due_at`` (and ``start_*``) in their own slots via
    :class:`DueDate`/:class:`StartDate`, and carries ``completed_at`` verbatim.
    A workspace is read only if the object states one -- it is never derived
    from the project memberships.

    Raises :class:`AsanaFieldError` for a structurally malformed object (a
    missing/blank ``gid``, a non-list ``projects``). It does not interpret a
    vendor ``errors`` array: a non-2xx response never reaches here (that is
    routed to :mod:`kiro_crew.connections.vendors.asana.errors` first).
    """

    if not isinstance(obj, Mapping):
        raise AsanaFieldError(f"task object must be a mapping, got {type(obj).__name__}")
    gid = normalize_gid(obj.get("gid"), what="task gid")
    name = obj.get("name")
    if name is not None and not isinstance(name, str):
        raise AsanaFieldError("task name must be a string when present")

    raw_projects = obj.get("projects", [])
    if not isinstance(raw_projects, list):
        raise AsanaFieldError("task 'projects' must be an array")
    project_gids = normalize_gid_set(
        (_membership_gid(row) for row in raw_projects),
        what="project",
    )

    workspace = obj.get("workspace")
    workspace_gid = None
    if isinstance(workspace, Mapping):
        workspace_gid = normalize_gid(workspace.get("gid"), what="workspace gid")
    elif isinstance(workspace, str) and workspace.strip():
        workspace_gid = normalize_gid(workspace, what="workspace gid")

    completed = bool(obj.get("completed", False))
    completed_at = obj.get("completed_at")
    if completed_at is not None and not isinstance(completed_at, str):
        raise AsanaFieldError("completed_at must be a datetime string or null")

    assignee = _optional_ref_gid(obj.get("assignee"), "assignee")
    parent = _optional_ref_gid(obj.get("parent"), "parent")

    return Task(
        gid=gid,
        name=name or "",
        workspace=workspace_gid,
        projects=project_gids,
        due=DueDate.from_read(obj.get("due_on"), obj.get("due_at")),
        start=StartDate.from_read(obj.get("start_on"), obj.get("start_at")),
        assignee=assignee,
        parent=parent,
        completed=completed,
        completed_at=completed_at,
    )


def _membership_gid(row: object) -> str:
    """Extract a project GID from a task's ``projects[]`` entry.

    Asana returns compact ``{gid, resource_type, name}`` objects; a bare string
    GID is also accepted for a caller-built object. Anything else is a shape
    fault.
    """

    if isinstance(row, Mapping):
        return normalize_gid(row.get("gid"), what="project gid")
    if isinstance(row, str):
        return normalize_gid(row, what="project gid")
    raise AsanaFieldError(
        f"project membership entry must be an object or GID string, got {type(row).__name__}"
    )


def _optional_ref_gid(row: object, what: str) -> Optional[str]:
    """Extract a GID from an optional ``{gid: ...}`` reference, or None."""

    if row is None:
        return None
    if isinstance(row, Mapping):
        return normalize_gid(row.get("gid"), what=f"{what} gid")
    if isinstance(row, str) and row.strip():
        return normalize_gid(row, what=f"{what} gid")
    raise AsanaFieldError(f"{what} must be a reference object, GID string, or null")


@dataclass(frozen=True)
class Project:
    """A normalized read of an Asana project object.

    A project resolves to exactly one workspace (its containing workspace).
    That workspace is carried explicitly and is NOT the mechanism by which a
    task's workspace is derived -- the connector keeps the two facts separate.
    """

    gid: str
    name: str
    workspace: Optional[str]
    archived: bool


def parse_project(obj: Mapping[str, Any]) -> Project:
    """Read a raw Asana project JSON object into a :class:`Project`."""

    if not isinstance(obj, Mapping):
        raise AsanaFieldError(f"project object must be a mapping, got {type(obj).__name__}")
    gid = normalize_gid(obj.get("gid"), what="project gid")
    name = obj.get("name")
    if name is not None and not isinstance(name, str):
        raise AsanaFieldError("project name must be a string when present")
    workspace = obj.get("workspace")
    workspace_gid = None
    if isinstance(workspace, Mapping):
        workspace_gid = normalize_gid(workspace.get("gid"), what="workspace gid")
    elif isinstance(workspace, str) and workspace.strip():
        workspace_gid = normalize_gid(workspace, what="workspace gid")
    return Project(
        gid=gid,
        name=name or "",
        workspace=workspace_gid,
        archived=bool(obj.get("archived", False)),
    )
