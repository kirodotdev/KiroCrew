"""Shared Crew: multi-person attribution, fair admission and collaboration.

This package holds the frozen cross-lane contracts (``contracts``,
``admission``, ``collab``) and the shared record codec (``codec``). Behaviour
modules live beside them and import these types; they never redefine a field.
The owning spec is ``docs/system-specs/modules/shared-crew.md``.
"""

from kiro_crew.shared_crew.codec import SCHEMA_VERSION, ContractError

__all__ = ["SCHEMA_VERSION", "ContractError"]
