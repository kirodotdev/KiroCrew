"""The survey "new user" counter increments only on genuine user chats.

``DashboardState.get_or_create_slot`` is the sole place a brand-new slot is
minted. This asserts the durable session-pulse counter goes up by one only when
the caller both opts in via ``count_user_session=True`` (the human request-layer
paths: chat-send auto-create, new-chat tab, fork) AND the new slot's origin is
``SlotOrigin.USER``. Either conjunct alone must not count: origin=USER without
the flag is the agent-driven session-control create verb (#6139), and the flag
without USER origin is an app/cron/system slot the survey must never see.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard import session_pulse_counter as spc
from kiro_crew.dashboard.state import DashboardState, SlotOrigin
from kiro_crew.history import ConversationLog

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"

# Origin shapes that must never count, flag or not.
_NON_USER_KWARGS = (
    {"origin": SlotOrigin.CRON},
    {"origin": SlotOrigin.SYSTEM},
    {"app": "some-app"},  # resolves to APP origin
    {},  # untagged (origin="")
)


@pytest.fixture(autouse=True)
def _isolated_counter(tmp_path, monkeypatch: pytest.MonkeyPatch):
    counter_dir = tmp_path / "home"
    counter_dir.mkdir()
    # Both the counter module and state resolve config_dir(); point both at a
    # throwaway dir so the counter file and any open-slots snapshot stay local.
    monkeypatch.setattr(spc, "config_dir", lambda: counter_dir)
    import kiro_crew.dashboard.state as state_mod

    monkeypatch.setattr(state_mod, "config_dir", lambda: counter_dir, raising=False)
    return counter_dir


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path / "log"),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def test_user_origin_new_chat_with_flag_increments(tmp_path) -> None:
    state = _make_state(tmp_path)
    assert spc.get_user_session_count() == 0
    state.get_or_create_slot(origin=SlotOrigin.USER, count_user_session=True)
    assert spc.get_user_session_count() == 1
    state.get_or_create_slot(origin=SlotOrigin.USER, count_user_session=True)
    assert spc.get_user_session_count() == 2


def test_user_origin_without_flag_does_not_increment(tmp_path) -> None:
    # THE regression pinned by #6139: the session-control create verb mints
    # brand-new slots with origin=SlotOrigin.USER (the tag carries slots:user
    # privacy semantics and cannot change) but does NOT opt in to the counter.
    # An agent opening sessions unattended must not satisfy the survey's
    # eligibility window on its own. This is exactly the session-control call
    # shape: unnamed slot, origin=USER, flag left at its default.
    state = _make_state(tmp_path)
    state.get_or_create_slot(None, agent="some-agent", origin=SlotOrigin.USER)
    assert spc.get_user_session_count() == 0
    # Mutation guard: dropping the ``count_user_session`` conjunct from the
    # increment condition in state.py makes this fail.
    state.get_or_create_slot(origin=SlotOrigin.USER)
    assert spc.get_user_session_count() == 0


@pytest.mark.parametrize("kwargs", _NON_USER_KWARGS)
def test_flag_without_user_origin_does_not_increment(tmp_path, kwargs) -> None:
    # The origin conjunct is the invariant floor: a caller can never count a
    # non-USER slot, even when it passes the flag. Mutation guard: dropping the
    # ``slot._origin == SlotOrigin.USER`` conjunct makes this fail.
    state = _make_state(tmp_path)
    state.get_or_create_slot(count_user_session=True, **kwargs)
    assert spc.get_user_session_count() == 0


@pytest.mark.parametrize("kwargs", _NON_USER_KWARGS)
def test_non_user_origins_do_not_increment(tmp_path, kwargs) -> None:
    state = _make_state(tmp_path)
    state.get_or_create_slot(**kwargs)
    assert spc.get_user_session_count() == 0


def test_restore_shape_named_user_slot_does_not_increment(tmp_path) -> None:
    # Restore/rehydrate calls get_or_create_slot with the persisted key as
    # `name` and origin=USER. That must NOT count -- otherwise every gateway
    # restart re-counts each restored user session. Regression for the GPT
    # blocking finding "restoring sessions corrupts the durable session count".
    # The flag does not override this: even an opted-in caller addressing a
    # named (non-minted) slot stays uncounted.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(
        name="chat-9-1786589233", origin=SlotOrigin.USER, count_user_session=True
    )
    assert spc.get_user_session_count() == 0
    # Returning the now-existing slot also does not count.
    again = state.get_or_create_slot(
        name="chat-9-1786589233", origin=SlotOrigin.USER, count_user_session=True
    )
    assert again is slot
    assert spc.get_user_session_count() == 0


# ---------------------------------------------------------------------------
# Structural pins on the call sites. The behavioral tests above exercise
# get_or_create_slot directly; these pin WHICH callers opt in, so mutating a
# call site (e.g. passing True at the session-control create verb, or dropping
# the flag from a human path) fails a test without needing a full HTTP stack.
# ---------------------------------------------------------------------------


def _opted_in_call_counts() -> dict[str, int]:
    """Map of module path (relative to src/kiro_crew) -> number of
    ``get_or_create_slot(...)`` calls passing a truthy ``count_user_session``,
    swept over the whole package so a new opt-in anywhere is caught."""
    counts: dict[str, int] = {}
    for path in sorted(_SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "count_user_session" not in text:
            continue
        tree = ast.parse(text)
        n = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "get_or_create_slot":
                continue
            for kw in node.keywords:
                if kw.arg == "count_user_session" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is False
                ):
                    n += 1
        if n:
            # as_posix() so the keys are stable across OSes (Windows yields
            # backslashes from relative_to + str).
            counts[path.relative_to(_SRC).as_posix()] = n
    return counts


def test_only_human_request_paths_opt_in() -> None:
    # Exactly the three human request-layer paths carry the flag: the chat-send
    # auto-create and the new-chat tab (chat_handlers.py), and fork
    # (chat_fork.py). This sweeps every module under src/kiro_crew, so an
    # opt-in appearing anywhere else -- most importantly the session-control
    # create verb, whose absence IS the fix for #6139 -- or disappearing from
    # these two files is a deliberate decision: update this pin alongside it.
    assert _opted_in_call_counts() == {
        "dashboard/chat_handlers.py": 2,
        "dashboard/chat_fork.py": 1,
    }


def test_session_control_create_does_not_opt_in() -> None:
    # The named regression for #6139, kept explicit even though the sweep above
    # subsumes it: the session-control create verb mints USER-origin slots
    # (privacy semantics) but must not count toward the survey. Passing
    # count_user_session=True there re-introduces the bug.
    assert "dashboard/session_control.py" not in _opted_in_call_counts()
