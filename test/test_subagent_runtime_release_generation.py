"""The conditional companion-runtime release (`release_subagent_runtime`).

The registry is keyed by parent session key alone, so a release that outlives its own
teardown pops and kills whatever is registered when it finally runs. A slow
`provider.shutdown()` makes that ordinary: the teardown's `finally` can reach the release
after a successor turn has already spawned its own runtime under the same key, and the kill
then destroys work the user never asked to end.

`expected_generation` is the guard, using the same captured-generation idiom as
`SessionManager.destroy_if`. Every test here drives the boundary method directly against a
recording runtime, so nothing real is spawned or signalled.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import session
from kiro_crew.config import KiroCrewConfig


def _manager() -> session.SessionManager:
    cfg = KiroCrewConfig()
    cfg.session.pool_size = 0
    return session.SessionManager(cfg, provider_factory=None)


def _recording_runtime(killed: list[str], name: str) -> SimpleNamespace:
    async def _kill(*, expected: bool = False, reason: str = "") -> None:
        killed.append(name)

    return SimpleNamespace(kill=_kill, is_alive=lambda: True)


KEY = "channel:ch1:a1"


@pytest.mark.asyncio
async def test_a_release_that_outlived_its_teardown_spares_the_successor():
    mgr = _manager()
    killed: list[str] = []

    teardown_generation = mgr._advance_session_generation(KEY)
    # The successor: a new turn published under the same key, which advances the generation,
    # then registered its own runtime.
    mgr._advance_session_generation(KEY)
    successor = _recording_runtime(killed, "successor")
    mgr._subagent_runtimes[KEY] = successor

    await mgr.release_subagent_runtime(KEY, expected_generation=teardown_generation)

    assert killed == [], f"the abandoned release killed a successor's runtime; killed={killed}"
    assert mgr._subagent_runtimes.get(KEY) is successor, (
        "the successor's runtime was unregistered by a release that belonged to an earlier "
        "teardown, so its next lookup cold-starts a replacement"
    )


@pytest.mark.asyncio
async def test_a_release_matching_its_own_generation_still_kills():
    """The positive control: the guard must not silence the release it belongs to."""
    mgr = _manager()
    killed: list[str] = []

    teardown_generation = mgr._advance_session_generation(KEY)
    mine = _recording_runtime(killed, "mine")
    mgr._subagent_runtimes[KEY] = mine

    await mgr.release_subagent_runtime(KEY, expected_generation=teardown_generation)

    assert killed == ["mine"], f"the release skipped its own runtime; killed={killed}"
    assert KEY not in mgr._subagent_runtimes, "and it must leave the registry empty"


@pytest.mark.asyncio
async def test_an_unconditional_release_is_unchanged():
    """Callers that pass no generation keep the pre-existing behaviour."""
    mgr = _manager()
    killed: list[str] = []

    mgr._advance_session_generation(KEY)
    mgr._advance_session_generation(KEY)
    mgr._subagent_runtimes[KEY] = _recording_runtime(killed, "whatever")

    await mgr.release_subagent_runtime(KEY)

    assert killed == ["whatever"], f"an unconditional release must still kill; killed={killed}"
    assert KEY not in mgr._subagent_runtimes
