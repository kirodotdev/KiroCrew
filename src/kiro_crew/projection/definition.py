"""What a projection IS, declared without importing the driver that runs it.

A projection is a small pure fold. Each unit owns a ``key``, a ``state_version``,
an ``init()`` that returns fresh empty state, an ``apply(state, event) -> state``
and a ``view(state) -> dict``.

THE SAME-REFERENCE RULE, which is what makes change detection cheap: ``apply``
MUST return the SAME object (by identity) when the event does not affect the unit.
:class:`~kiro_crew.projection.registry.ProjectionRegistry` compares with ``is``,
so an unchanged identity emits no change callback and a new object emits one.

The two ways to break that rule fail in OPPOSITE directions and neither raises,
which is why the rule is stated here rather than left to the driver. Returning a
fresh but EQUAL object is a spurious change, pushed to every client watching that
store for an event the unit ignored. MUTATING the state in place and returning it
is the worse one: identity is unchanged, so a real change is DROPPED and readers
keep a stale value until some later event happens to move the fold. So state is
treated as immutable -- ``apply`` returns a new object or the original, never a
modified original.

This module is separate from the registry so a domain can DECLARE its folds
without importing the driver. The declaration is the part other code reads, and a
fold is testable by calling ``init`` / ``apply`` / ``view`` with no registry in
the picture.

``event`` is annotated ``Any`` deliberately. The kernel never reads inside an
event: each client owns its own carrier type -- the member log's ``Event``
TypedDict, the crew log's ``Entry`` dataclass -- those types stay in those
packages, and the registry learns an event's ``seq`` through a reader its client
supplied rather than by reaching into the payload. A definition SHOULD annotate
its own ``apply`` with its own carrier type; that is where the carrier belongs.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProjectionDefinition(Protocol):
    """One named fold: an identity, a starting state, one step per event, a render.

    ``key`` names the view and is unique within a registry. ``state_version``
    describes the SHAPE of what ``init`` and ``apply`` store, so a persisted state
    written by another build can be told apart from one this build understands --
    it is the definition's own number, not the registry's.
    """

    key: str
    state_version: int

    def init(self) -> Any: ...
    def apply(self, state: Any, event: Any) -> Any: ...
    def view(self, state: Any) -> dict: ...
