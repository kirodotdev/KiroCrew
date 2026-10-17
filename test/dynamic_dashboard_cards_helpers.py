"""Fixtures the automatic dashboard card test modules share.

The autouse ``_seeded_session_tree`` (a seeded, empty session tree and a crew log that is
on), the ``lifecycle`` card service over one root slot (declared
``@pytest.fixture(name="lifecycle")`` and imported as ``lifecycle_fixture``), ``_parked``
(a generation or turn held open until the code under test cancels it) and
``_worker_done``. ``test_dynamic_dashboard_cards.py`` and
``test_dynamic_dashboard_cards_activation.py`` import them by name.
"""

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.dynamic_cards import CardBudget
from kiro_crew.testing.wait import await_until


async def _parked() -> None:
    """Hold a fake generation or turn open until the code under test cancels it.

    Unbounded on purpose: every test that parks one also awaits the cancellation it
    expects (``cancelling`` / ``cancelled`` / the worker itself) under the lost-run
    bound, so a park that is never cancelled fails by name there, not here.
    """
    # flake-ok: pre-existing unbounded park that only the code under test's cancellation ends; every caller awaits that cancellation under the lost-run bound, moved verbatim; tracked for a de-flake pass
    await asyncio.Event().wait()


async def _worker_done(service) -> None:
    """Wait for the lifecycle service's current worker to finish, under the lost-run bound."""
    await await_until(service.worker, what="the card worker finishing")


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the automatic card modules' autouse seeded session tree with the crew log on, moved verbatim; none of them calls monkeypatch.undo()
def _seeded_session_tree(monkeypatch):
    """A seeded, empty session tree and a crew log that is on, for every case.

    The root gate refuses an unseeded tree, and no card is generated with the log off,
    which is this suite's environment default.
    """
    from kiro_crew.crew_log import session_tree_projection
    from kiro_crew.dashboard import card_lifecycle

    monkeypatch.setattr(card_lifecycle, "crew_log_enabled", lambda: True)

    session_tree_projection.reset_for_tests()
    session_tree_projection.projection().ensure_seeded()
    yield
    session_tree_projection.reset_for_tests()


@pytest.fixture(name="lifecycle")
def lifecycle_fixture(monkeypatch):
    """One root slot, with the two process-global reads its card depends on PINNED.

    Every automatic card is now a root session's card, so its numbers are folded from
    that session's crew log. The fold read is pinned to a small readable board so a case
    here never depends on what some sibling left in the store, and the session-tree
    projection is reset because ``_eligible`` refuses a slot the tree calls a worker.
    """
    from kiro_crew.crew_log import session_tree_projection
    from kiro_crew.dashboard import card_lifecycle
    from kiro_crew.history import TranscriptWithheld

    session_tree_projection.reset_for_tests()
    session_tree_projection.projection().ensure_seeded()
    monkeypatch.setattr(
        card_lifecycle,
        "_read_card_folds",
        lambda key: {
            "status": {"lifecycle": "open", "turns_completed": 2},
            "usage": {},
            "approvals": {},
            "work": {"items": [{"state": "open", "status": "progress"}], "omitted": 0},
        },
    )
    # The cases here are about publication, redaction and authority, and their model
    # replies are one-line layouts. Whether a layout binds every fact has its own cases
    # in the crew-main contract suite, so that one rule is held open here.
    monkeypatch.setattr(card_lifecycle, "_layout_hides_a_fact", lambda html: False)

    class Log:
        allowed = True
        present = True
        generation = 0

        @contextmanager
        def publication_hold(self, key):
            if not self.allowed:
                raise TranscriptWithheld("private")
            yield

        def session_mtime(self, key):
            return 1 if self.present else None

        def rotation_generation(self, key):
            return self.generation

        def chained_keys(self, key):
            return [key]

        def derive_recent(self, key, max_messages, roles=None):
            rows = getattr(self, "rows", slot.messages)
            if roles:
                rows = [row for row in rows if row.get("role") in roles]
            return rows[-max_messages:]

    slot = SimpleNamespace(
        key="one",
        _dashboard_card_identity="owner-one",
        memory_mode="persistent",
        messages=[
            {"role": "user", "content": "Build a release"},
            {"role": "assistant", "content": "Tests failed; investigating"},
        ],
    )
    state = SimpleNamespace(
        _slots={"one": slot},
        conversation_log=Log(),
        flush_slot_now=lambda slot: None,
        sessions=object(),
        _background_tasks=set(),
        broadcast_ws_owners=lambda *args: None,
    )
    cfg = SimpleNamespace(
        agent=SimpleNamespace(resolve_model=lambda role: "auto"),
        dashboard=SimpleNamespace(dynamic_dashboard_cards=True, language=""),
    )
    monkeypatch.setattr(card_lifecycle.KiroCrewConfig, "load", lambda: cfg)
    service = card_lifecycle.CardLifecycle(state, enabled=True)
    service.publisher.budget = CardBudget(debounce=0)
    return service, slot, state
