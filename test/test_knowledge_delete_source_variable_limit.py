"""Regression: deleting a source that owns more items than SQLite's variable limit.

``delete_source_cascade`` removes the items only this source holds with IN-lists
over their ids. One placeholder per id raised ``too many SQL variables`` once a
source owned more items than ``SQLITE_MAX_VARIABLE_NUMBER`` (32766 on 3.32+,
999 before), and the whole delete rolled back. The connection's limit is pinned
to the 999 floor so a small source reproduces it on every build.

The ``store`` fixture comes from ``knowledge_helpers.py``.
"""

from __future__ import annotations

import pytest
from knowledge_helpers import store_fixture  # noqa: F401
from test_knowledge_graph_search_variable_limit import _capped_variable_limit

from kiro_crew._sqlite_compat import sqlite3

_ITEMS = 1_200  # over the pinned 999 limit


def test_delete_source_with_more_items_than_the_variable_limit(store):
    sid = store.add_source("big", "folder", "/tmp/big")
    for i in range(_ITEMS):
        store.add_item(f"Doc {i}", f"text {i}", "doc", source_id=sid)

    with _capped_variable_limit(store):
        try:
            store.delete_source_cascade(sid)
        except sqlite3.OperationalError as exc:
            pytest.fail(f"deleting a {_ITEMS}-item source raised: {exc}")

    assert store.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    assert store.db.execute("SELECT COUNT(*) FROM sources WHERE id = ?", (sid,)).fetchone()[0] == 0
