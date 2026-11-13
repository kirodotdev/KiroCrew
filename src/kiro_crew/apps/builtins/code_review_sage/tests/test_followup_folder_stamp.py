"""The follow-up folder row carries the same ``created_at`` stamp as core's folder rows.

The sidebar's ``created`` folder sort orders on this field, and Kiro Crew core's
``test/test_chat_folder_created_at.py`` pins that every core folder-row constructor
writes it as epoch seconds on one line shape. The follow-up folder this app mints is
one more row in that sidebar, so it is held to the same line: a row without the stamp
would sort as older than everything, silently.
"""

from __future__ import annotations

import inspect

from kiro_crew.apps.builtins.code_review_sage.backend import routes as sage_routes

_STAMP_LINE = '"created_at": time.time()'


def test_the_followup_folder_row_writes_the_core_stamp() -> None:
    assert _STAMP_LINE in inspect.getsource(sage_routes._ensure_followup_folder)
