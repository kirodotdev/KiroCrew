"""A chat folder's ``default_crew``: which registered crew its new chats start on.

The field holds the instance id of one registered crew (the Remote crew
sessions preview), and ``""`` means Local, the same as a folder that never
carried the key. It is stored and returned verbatim: the dashboard reads it to
route the folder's New chat to *New chat on crew*, and nothing server-side
creates a session from it, so a crew that was removed since leaves a value that
simply names no crew.
"""

from __future__ import annotations

#: Longest ``default_crew`` kept. The field names a registered crew by its
#: instance id, which is short; the cap only bounds what a hand-written request
#: can store, and a longer value matches no crew so it is inert either way.
MAX_DEFAULT_CREW_LEN = 128


def clean_default_crew(value: object) -> str:
    """Normalise a folder's ``default_crew``: stripped, capped, ``""`` for Local.

    Only a string names a crew. ``None``, a number, a list or an object is read
    as "no crew" instead of being stringified into an id that no crew carries.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()[:MAX_DEFAULT_CREW_LEN]
