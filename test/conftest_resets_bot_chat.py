"""Per-test resets of process-global ``kiro_crew.bot_chat_app_tools`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import copy
import sys

from conftest_guards import per_test_guard


def _bot_chat_app_tools_tables(module):
    return [
        (name, value)
        for name, value in vars(module).items()
        if name.startswith("_")
        and not name.startswith("__")
        and isinstance(value, (dict, set, list))
    ]


def _cancel_bot_chat_close_timers(module):
    for handle in list(module._PENDING_CLOSE_TIMERS.values()):
        cancel = getattr(handle, "cancel", None)
        if cancel is not None:
            cancel()


def wipe_bot_chat_app_tools_state(module, setattr):
    """Empty every module-level table of *module*, cancel its close-retry timers and
    unset ``_RECONCILER`` and ``_CHANGE_EPOCH``; return the tables it emptied.

    The tables are found by type, so a table added to the module is covered
    without editing a list. Each is emptied in place and also swapped for an empty
    copy through *setattr* (a monkeypatch's), so undoing that monkeypatch puts back
    the module's own, now empty, objects.
    """
    _cancel_bot_chat_close_timers(module)
    originals = _bot_chat_app_tools_tables(module)
    for name, value in originals:
        value.clear()
        setattr(module, name, copy.copy(value))
    setattr(module, "_RECONCILER", None)
    setattr(module, "_CHANGE_EPOCH", 0)
    return [value for _name, value in originals]


@per_test_guard
def _fresh_bot_chat_app_tools_state(_floor_monkeypatch):
    """Give every test empty Bot chat app tables and no app reconciler.

    ``kiro_crew.bot_chat_app_tools`` keeps its mounts, offers, pending closes,
    projections and fences in module-level dicts and sets, plus ``_RECONCILER``
    (a reconciler and the loop it runs on) and ``_CHANGE_EPOCH``. A test that
    mounts a Bot chat or arms a close retry would otherwise hand those to every
    later test on the same xdist worker, including a ``_RECONCILER`` bound to a
    loop that test already closed.

    ``wipe_bot_chat_app_tools_state`` does the work, through ``_floor_monkeypatch``
    so a test's ``monkeypatch.undo()`` cannot bring a dirty table back. At teardown
    the retry timers are cancelled again and the tables it emptied are emptied
    once more. A test that never imported the module pays one dict lookup.
    """
    module = sys.modules.get("kiro_crew.bot_chat_app_tools")
    fence = sys.modules.get("kiro_crew.bot_chat_ending_fence")
    if fence is not None:
        for name, value in _bot_chat_app_tools_tables(fence):
            value.clear()
            _floor_monkeypatch.setattr(fence, name, copy.copy(value))
    if module is None:
        yield
        return
    originals = wipe_bot_chat_app_tools_state(module, _floor_monkeypatch.setattr)
    try:
        yield
    finally:
        _cancel_bot_chat_close_timers(module)
        for value in originals:
            value.clear()
