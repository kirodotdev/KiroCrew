"""Per-caller rate limiting on the dashboard's creation verbs.

This is the primary guard on an auto-approved create: the capacity ceilings bound
how much can exist, this bounds how fast one caller may make it. It is also the
guard that has to hold without durable state, so the window semantics and the
fail-closed paths are pinned here rather than left to the call sites.
"""

from __future__ import annotations

import pytest

from kiro_crew.dashboard import create_rate_limit as rl


@pytest.fixture(autouse=True)
def _clean_buckets():
    rl.reset_for_tests()
    yield
    rl.reset_for_tests()


def test_a_caller_gets_its_budget_and_then_is_refused() -> None:
    """Mutation guard: drop the length test and the last call still returns True."""
    for i in range(rl.MAX_SESSION_CREATES_PER_WINDOW):
        assert rl.allow_create(rl.SESSION_CREATE, "chat-1", now=1000.0), f"call {i} in budget"

    assert rl.allow_create(rl.SESSION_CREATE, "chat-1", now=1000.0) is False


def test_the_budget_refills_as_the_window_slides() -> None:
    """A refused caller must recover, or one burst disables the verb for good."""
    for _ in range(rl.MAX_SESSION_CREATES_PER_WINDOW):
        rl.allow_create(rl.SESSION_CREATE, "chat-1", now=1000.0)
    assert rl.allow_create(rl.SESSION_CREATE, "chat-1", now=1000.0) is False

    # One tick past the window, every timestamp has aged out.
    assert rl.allow_create(rl.SESSION_CREATE, "chat-1", now=1000.0 + rl.WINDOW_SECS + 1) is True


def test_one_caller_exhausting_its_budget_does_not_refuse_another() -> None:
    """Buckets are per caller; a shared bucket would let one agent mute everyone."""
    for _ in range(rl.MAX_SESSION_CREATES_PER_WINDOW):
        rl.allow_create(rl.SESSION_CREATE, "chat-hog", now=1000.0)

    assert rl.allow_create(rl.SESSION_CREATE, "chat-hog", now=1000.0) is False
    assert rl.allow_create(rl.SESSION_CREATE, "chat-other", now=1000.0) is True


def test_the_two_verbs_have_separate_budgets() -> None:
    """A folder burst must not consume the caller's session budget, or filing a
    goal's folder could stop it opening the sessions that goal needs."""
    for _ in range(rl.MAX_FOLDER_CREATES_PER_WINDOW):
        rl.allow_create(rl.FOLDER_CREATE, "chat-1", now=1000.0)

    assert rl.allow_create(rl.FOLDER_CREATE, "chat-1", now=1000.0) is False
    assert rl.allow_create(rl.SESSION_CREATE, "chat-1", now=1000.0) is True


def test_an_empty_caller_key_is_refused() -> None:
    """Fails closed. An unattributable request cannot be rate-limited at all, and
    bucketing those under one sentinel would still pass a whole budget on a key an
    attacker can blank."""
    assert rl.allow_create(rl.SESSION_CREATE, "", now=1000.0) is False


def test_an_unknown_verb_is_refused() -> None:
    """Fails closed, so a future verb wired up with a typo'd name is throttled to
    zero rather than silently unlimited."""
    assert rl.allow_create("session_delete", "chat-1", now=1000.0) is False


def test_a_dispatch_round_for_a_real_goal_fits_in_one_window() -> None:
    """The budgets must not bind honest work: a decomposed goal files one folder and
    opens roughly ten sessions in its dispatch round.

    Mutation guard: lower MAX_SESSION_CREATES_PER_WINDOW under ~12 and this fails,
    which is the signal that the limit has been tightened into the legitimate path.
    """
    assert rl.allow_create(rl.FOLDER_CREATE, "chat-conductor", now=1000.0) is True
    for i in range(12):
        assert rl.allow_create(
            rl.SESSION_CREATE, "chat-conductor", now=1000.0 + i
        ), "a 12-item goal must dispatch without being throttled"


def test_stale_buckets_are_swept_so_the_map_cannot_grow_without_bound() -> None:
    """Session keys churn for the gateway's lifetime, so buckets must be evicted --
    the reason this does not reuse the never-evicting AppRateLimiter."""
    for i in range(50):
        rl.allow_create(rl.SESSION_CREATE, f"chat-{i}", now=1000.0)
    assert len(rl._buckets) == 50

    # A call a full window later sweeps every aged-out bucket, keeping only its own.
    rl.allow_create(rl.SESSION_CREATE, "chat-late", now=1000.0 + rl.WINDOW_SECS + 1)
    assert len(rl._buckets) == 1, "aged-out buckets must not accumulate"
