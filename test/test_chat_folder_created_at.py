"""The ``created_at`` stamp on chat folder rows.

The sidebar's ``created`` folder sort orders on this field, so every place that
mints a folder row has to write it, and write it the same way -- epoch seconds as
a JSON number, which is the shape both the sidebar's ``folderCreated`` and the MCP
tree's ``_chat_folder_created`` read. A creator that forgot the stamp would sort
its folders as older than everything, silently.
"""

from __future__ import annotations

import inspect
import time

from kiro_crew.apps.builtins.code_review_sage.backend import routes as sage_routes
from kiro_crew.dashboard import arrival_folders, channel_folders, chat_folders

_STAMP_LINE = '"created_at": time.time()'


def test_an_arrival_row_carries_a_live_epoch_stamp() -> None:
    before = time.time()
    row = arrival_folders._new_folder("from mac", "", 0)
    after = time.time()
    assert isinstance(row["created_at"], float)
    assert before <= row["created_at"] <= after


def test_an_arrival_row_still_reads_as_untouched_with_the_stamp() -> None:
    """The rollback guard treats a key ``_new_folder`` emits as view state, not as
    a person's edit -- so stamping must not pin an empty auto-created row."""
    row = arrival_folders._new_folder("from mac", "parent", 3)
    assert arrival_folders._is_untouched_arrival_row(row, name="from mac", parent_id="parent")


def test_every_folder_row_constructor_writes_the_same_stamp() -> None:
    """Source-level mirror, the way the arrival module pins the name clip: each
    of the four row constructors -- plain dict literals in four modules -- writes
    the stamp on the same line shape, so the two sort keys agree on the field.
    The tuple is the roster, not a discovery: a fifth constructor is caught by
    the reviewer who adds it here, not by this test."""
    constructors = (
        chat_folders.create_folder_record,
        arrival_folders._new_folder,
        channel_folders.ensure_channel_folder,
        sage_routes._ensure_followup_folder,
    )
    for fn in constructors:
        assert _STAMP_LINE in inspect.getsource(fn), fn.__qualname__
