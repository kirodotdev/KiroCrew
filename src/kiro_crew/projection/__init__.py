"""The projection kernel: fold an append-only log into named derived views.

Two pieces, and a client imports the ones it needs:

* :mod:`~kiro_crew.projection.definition` -- what a fold IS, declarable without
  importing the driver that runs it.
* :mod:`~kiro_crew.projection.registry` -- the driver: per-store cells, the
  watermark that makes folding idempotent, and the change feed.

The kernel owns no carrier and no path. An event is an opaque payload whose ``seq``
is read through a client-supplied reader, so the member log's ``Event`` TypedDict
and the crew log's ``Entry`` dataclass drive the same registry while each type
stays in the package that owns it.
"""

from kiro_crew.projection.definition import ProjectionDefinition
from kiro_crew.projection.registry import (
    OnChange,
    ProjectionRegistry,
    SeqOf,
    attribute_seq,
    mapping_seq,
)

__all__ = [
    "OnChange",
    "ProjectionDefinition",
    "ProjectionRegistry",
    "SeqOf",
    "attribute_seq",
    "mapping_seq",
]
