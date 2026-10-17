"""A stored reference to one registered crew, by its instance id.

Used by ``instances.default_destination`` and a chat folder's
``default_destination_crew``: both name the crew that *Send a copy to* lists
first. ``""`` means no default. The value is stored verbatim and resolved
against the registry only when the menu renders, so a crew removed since leaves
a value that simply names no crew and changes nothing.

This module imports nothing from ``kiro_crew`` so the config builders can use it
without an import cycle.
"""

from __future__ import annotations

#: Longest crew reference kept. Instance ids are short; the cap only bounds what
#: a hand-written request or config can store, and a longer value matches no
#: crew, so it is inert either way.
MAX_CREW_REF_LEN = 128


def clean_crew_ref(value: object) -> str:
    """Normalise a crew reference: stripped, capped, ``""`` for none.

    Only a string names a crew. ``None``, a number, a list or an object is read
    as "no crew" instead of being stringified into an id no crew carries.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_CREW_REF_LEN]
