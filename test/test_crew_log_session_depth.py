"""The ``depth`` field on ``session/opened``.

Covers the writer side only: what :func:`kiro_crew.crew_log.emit.on_session_opened`
stores for the new ``depth`` argument, that the stored entry still validates, and
that an entry written WITHOUT a ``depth`` (every log from before this change)
replays through the declaration and the depth counter reader without error or a
miscount. The last section covers the caller that RESOLVES the depth a writer passes
(``_crew_log_tree_depth``), for the one answer the writer can give without walking
anything: a root. The counting over the tree lives in
``test_otel_session_tree_fold.py``, and the walk itself in
``test_crew_log_session_tree_projection.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.crew_log import crew_log_path, emit
from kiro_crew.crew_log.entry_types import validate_data
from kiro_crew.crew_log.otel_session_tree_fold import depth_observation

SESSION = "acp-sess-depth-0001"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Each test writes into its own data home; the writer is drained on the way out.

    Patches through ``_floor_monkeypatch`` rather than the shared ``monkeypatch`` so a
    test that calls ``monkeypatch.undo()`` mid-way cannot lift this fixture's
    ``KIROCREW_HOME`` pin (the Determinism contract's D11)."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _floor_monkeypatch.setattr(emit, "_retry_delay", lambda _attempts: 0.0)
    emit.reset_caches()
    yield
    emit.drain_for_shutdown(timeout=2.0)
    emit.reset_caches()
    # Opening a session seeds the session-tree projection and submits a debounced
    # checkpoint write to the shared maintenance pool. Settle it before pytest tears
    # down this home, or that write lands against a directory this fixture is about
    # to remove and the determinism floor flags a shared-pool leftover.
    from kiro_crew.executors import shutdown_maintenance_executor

    shutdown_maintenance_executor()


def _opened_data(session_id: str = SESSION) -> dict:
    """The one ``session/opened`` entry's ``data``, drained to disk and read back."""
    emit.drain_for_shutdown(timeout=2.0)
    path: Path = crew_log_path("session", session_id)
    assert path.is_file(), "no crew log was written"
    entries = [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]
    opened = [e for e in entries if e.get("type") == "session/opened"]
    assert len(opened) == 1, f"expected one session/opened entry, got {len(opened)}"
    return opened[0]["data"]


# --- what the writer stores ------------------------------------------------


@pytest.mark.parametrize(
    "depth",
    [
        pytest.param(2, id="test_a_valid_depth_is_stored"),
        # A root a caller KNOWS is a root says 0 on purpose. Zero is a real depth, not
        # the absence of one, so it must land in the entry.
        pytest.param(0, id="test_depth_zero_is_stored_as_a_fact"),
    ],
)
def test_a_known_depth_is_stored(depth):
    emit.on_session_opened(SESSION, slot="chat-7", depth=depth)
    assert _opened_data()["depth"] == depth


def test_no_depth_argument_writes_no_key():
    # The default. An unresolved depth is absent, never a zero -- an absent depth is
    # one session left out of a distribution, a wrong zero is an invented root.
    emit.on_session_opened(SESSION, slot="chat-7")
    assert "depth" not in _opened_data()


def test_a_negative_depth_is_refused_at_write():
    # No walk of a tree produces a negative. Writing it would be a number in an
    # append-only entry no later read can correct, so the writer drops it.
    emit.on_session_opened(SESSION, slot="chat-7", depth=-1)
    assert "depth" not in _opened_data()


def test_a_bool_depth_is_refused_at_write():
    # bool is an int in Python and would land as 0 or 1. It is not a depth.
    emit.on_session_opened(SESSION, slot="chat-7", depth=True)  # type: ignore[arg-type]
    assert "depth" not in _opened_data()


# --- the stored entry still validates --------------------------------------


def test_the_entry_with_a_depth_still_validates():
    emit.on_session_opened(SESSION, slot="chat-7", depth=3)
    # No raise: depth is a declared JSON_INT field, so the entry is well-formed.
    validate_data("session", "session/opened", _opened_data())


def test_the_entry_without_a_depth_still_validates():
    emit.on_session_opened(SESSION, slot="chat-7")
    validate_data("session", "session/opened", _opened_data())


# --- an old log (no depth) replays without error or miscount ---------------


def test_an_old_opened_entry_replays_without_a_depth_observation():
    # The exact shape of a session/opened written before this change: no depth key.
    # It must validate AND contribute no depth counter add -- not a zero.
    old = {
        "agent": "kirocrew",
        "slot": "chat-7",
        "model": "m",
        "cwd": "",
        "owner": "default",
        "resumed": False,
    }
    validate_data("session", "session/opened", old)
    assert depth_observation(old) is None


def test_a_new_opened_entry_round_trips_to_the_same_observation():
    # The depth the writer stored is the depth the depth counter reader observes.
    emit.on_session_opened(SESSION, slot="chat-7", depth=4)
    data = _opened_data()
    assert depth_observation(data) == 4


# --- the resolver's root answer is censored with the child's -----------------


class _BareSlot:
    """A slot that states no creator -- the shape the resolver reads as a root."""


def test_the_resolver_states_a_root_depth_on_a_readable_tree():
    # Imported through ``chat_runner``, which is where the facade mirror injects the
    # names ``crew_log_attribution`` reads at run time.
    from kiro_crew.crew_log import session_tree_projection as stp
    from kiro_crew.dashboard.chat_runner import _crew_log_tree_depth

    stp.reset_for_tests()
    stp.projection().ensure_seeded()
    assert _crew_log_tree_depth(_BareSlot(), "") == 0


def test_the_resolver_withholds_a_root_depth_when_the_tree_cannot_be_read():
    # The whole point of asking the tree for a number that needs no walk: a zero
    # written while every child's depth is withheld would leave the distribution
    # holding roots and nothing else.
    from kiro_crew.crew_log import session_tree_projection as stp
    from kiro_crew.dashboard.chat_runner import _crew_log_tree_depth

    stp.reset_for_tests()
    assert _crew_log_tree_depth(_BareSlot(), "") is None
