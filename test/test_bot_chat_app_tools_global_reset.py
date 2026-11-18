"""The conftest reset leaves no Bot chat app state behind.

The test writes into every module-level table of ``kiro_crew.bot_chat_app_tools``
in place, arms a close-retry timer, binds ``_RECONCILER`` to a closed loop and
bumps ``_CHANGE_EPOCH``, then runs ``wipe_bot_chat_app_tools_state`` -- the
function the per-test ``_fresh_bot_chat_app_tools_state`` guard runs -- and
reads everything clean. Drop any step of the wipe and a check here fails.
"""

from __future__ import annotations

import asyncio

from conftest_resets import wipe_bot_chat_app_tools_state

from kiro_crew import bot_chat_app_tools as tools


def _tables() -> dict[str, object]:
    return {
        name: value
        for name, value in vars(tools).items()
        if name.startswith("_")
        and not name.startswith("__")
        and isinstance(value, (dict, set, list))
    }


def test_tables_are_found():
    names = set(_tables())
    assert {
        "_MOUNTED",
        "_PENDING_CLOSE",
        "_PENDING_CLOSE_APPS",
        "_PENDING_CLOSE_TIMERS",
        "_REVOCATION_FENCE",
        "_HARNESS_OWED",
        "_SESSION_APP_SNAPSHOT",
    } <= names


def test_the_wipe_leaves_every_table_empty_and_every_timer_cancelled(monkeypatch):
    dirtied = _tables()
    for value in dirtied.values():
        if isinstance(value, dict):
            value[("leak", "leak")] = "leak"
        elif isinstance(value, set):
            value.add(("leak", "leak"))
        else:
            value.append("leak")
    loop = asyncio.new_event_loop()
    handle = loop.call_later(3600, lambda: None)
    tools._PENDING_CLOSE_TIMERS[("app", "atlas")] = handle
    loop.close()
    monkeypatch.setattr(tools, "_RECONCILER", (object(), loop))
    monkeypatch.setattr(tools, "_CHANGE_EPOCH", 7)

    emptied = wipe_bot_chat_app_tools_state(tools, monkeypatch.setattr)

    assert sorted(name for name, value in _tables().items() if value) == []
    assert sorted(name for name, value in dirtied.items() if value) == []
    assert {id(value) for value in emptied} == {id(value) for value in dirtied.values()}
    assert tools._RECONCILER is None
    assert tools._CHANGE_EPOCH == 0
    assert handle.cancelled()
