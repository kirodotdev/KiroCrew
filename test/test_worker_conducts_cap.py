# SPDX-License-Identifier: Apache-2.0
"""While a Bot's workers can conduct, its ``Workers at once`` always applies.

rfc-bot-workers-conduct.md section 5.5 and Phase 1 exit criterion 8: with the Bot's
``worker_conducts`` switch on and ``max_workers`` 0 (no limit), a ``session_create``
from the Bot's thread or from one of its conducting workers, a ``session_revive`` or
a ``session_fork`` that would open the Bot's fifth worker is refused
``bot_worker_cap_reached`` with nothing opened. With the switch off it is not, and a
limit the owner set applies as is. All three verbs ask
``mate_worker_conducts.effective_worker_cap_refusal``; ``bot_worker_cap.py`` is not
edited.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest
from test_mate_worker_dispatch import (  # noqa: F401
    _create,
    _fresh_create_budget,
    _start,
    _write_config,
    crewmate,
)

from kiro_crew import members
from kiro_crew.config.loader import KiroCrewAgentConfig, _invalidate_config_cache
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.session_control import SessionControlError
from kiro_crew.mate_worker import MATE_WORKER_AGENT

#: The exit criterion names the fifth worker: four open, one more refused. The new
#: names are imported inside the cases that read them, so every behaviour case
#: also runs, and fails, on a tree without them.
FOUR = 4


def _bot(tmp_path, **fields) -> None:
    """Write pixel's owner-set fields as the Profile's PUT would store them."""
    path = tmp_path / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["agents"]["pixel"].update(fields)
    _write_config(path, config)
    _invalidate_config_cache()


def _open_workers(state, caller, count: int) -> list[str]:
    return [_create(state, caller)["target"] for _ in range(count)]


def _refused(call) -> SessionControlError:
    with pytest.raises(SessionControlError) as err:
        call()
    assert err.value.code == "bot_worker_cap_reached"
    assert err.value.status == 409
    return err.value


def _with_history(state, key: str) -> None:
    state.get_slot(key).messages.extend(
        [{"role": "user", "content": "m0"}, {"role": "assistant", "content": "m1"}]
    )


def test_the_limit_is_four():
    from kiro_crew.config.sections_session import BOT_MAX_WORKERS_CONDUCTING

    assert BOT_MAX_WORKERS_CONDUCTING == FOUR


@pytest.mark.parametrize("on", [True, False])
def test_the_threads_fifth_create(crewmate, tmp_path, on):  # noqa: F811
    state, caller, _execution = crewmate
    _bot(tmp_path, worker_conducts=on)
    _open_workers(state, caller, FOUR)
    if not on:
        # Switch off: no limit, as today.
        assert _create(state, caller)["target"] in state._slots
        return
    before = set(state._slots)
    refusal = _refused(lambda: _create(state, caller))
    assert "allows 4" in str(refusal)
    assert set(state._slots) == before


def _conducting_worker(state, caller, tmp_path):
    """pixel's switch on; the thread's first worker, its session started with the kit."""
    _bot(tmp_path, worker_conducts=True)
    worker = state.get_slot(_create(state, caller)["target"])
    assert members.conducting_worker_session(slot_history_key(worker))
    _start(state, worker)
    return worker


def _template_child(state, worker) -> str:
    return _create(state, worker, agent=MATE_WORKER_AGENT, agent_kind="template")["target"]


@pytest.mark.parametrize("on", [True, False])
def test_a_conducting_workers_fifth_create(crewmate, tmp_path, on):  # noqa: F811
    """The limit is Bot-wide: the thread's workers and a worker's children share it.

    A conducting worker creates as its session started: with the switch turned off
    mid-task it is still held to four, until its next start (the reload the owner's
    save brings); from then on its creates follow the switch.
    """
    state, caller, _execution = crewmate
    worker = _conducting_worker(state, caller, tmp_path)
    _open_workers(state, caller, 2)
    _template_child(state, worker)
    _bot(tmp_path, worker_conducts=on)
    before = set(state._slots)
    _refused(lambda: _template_child(state, worker))
    assert set(state._slots) == before
    _start(state, worker)
    if on:
        _refused(lambda: _template_child(state, worker))
    else:
        assert _template_child(state, worker) in state._slots


def test_the_threads_creates_follow_the_live_switch(crewmate, tmp_path):  # noqa: F811
    """Only a session holding the kit keeps the bound past a switch-off: the thread's
    own fifth create opens once the switch is off, while its conducting worker,
    still running from before, is held to four."""
    state, caller, _execution = crewmate
    worker = _conducting_worker(state, caller, tmp_path)
    _open_workers(state, caller, 3)
    _bot(tmp_path, worker_conducts=False)
    _refused(lambda: _template_child(state, worker))
    assert _create(state, caller)["target"] in state._slots


def test_a_conducting_workers_fork_keeps_the_bound_until_its_next_start(
    crewmate, tmp_path  # noqa: F811
):
    state, caller, _execution = crewmate
    worker = _conducting_worker(state, caller, tmp_path)
    child = _template_child(state, worker)
    _with_history(state, child)
    _open_workers(state, caller, 2)
    _bot(tmp_path, worker_conducts=False)

    def fork():
        return asyncio.run(
            sc.fork_session(state, caller_session_key=slot_history_key(worker), source=child)
        )

    _refused(fork)
    _start(state, worker)
    assert fork()["target"] in state._slots


def test_a_conducting_workers_revive_keeps_the_bound_until_its_next_start(
    crewmate, tmp_path, monkeypatch  # noqa: F811
):
    state, caller, _execution = crewmate
    worker = _conducting_worker(state, caller, tmp_path)
    monkeypatch.setattr(sc, "_creation_parent", lambda key: worker.key)
    child = _template_child(state, worker)
    _with_history(state, child)
    state.get_slot(child)._dirty = True
    asyncio.run(sc.close_target(state, caller_session_key=slot_history_key(worker), target=child))
    meta = state.conversation_log.get_metadata(f"dashboard:{child}")
    state.conversation_log.update_metadata(
        f"dashboard:{child}", {"closed_at": float(meta["closed_at"]) - 60.0}
    )
    _open_workers(state, caller, 3)
    _bot(tmp_path, worker_conducts=False)

    def revive():
        return asyncio.run(
            sc.revive_session(state, caller_session_key=slot_history_key(worker), target=child)
        )

    _refused(revive)
    assert child not in state._slots
    _start(state, worker)
    revive()
    assert child in state._slots


@pytest.mark.parametrize("on", [True, False])
def test_a_fork_that_would_open_the_fifth(crewmate, tmp_path, on):  # noqa: F811
    state, caller, _execution = crewmate
    _bot(tmp_path, worker_conducts=on)
    first = _open_workers(state, caller, FOUR)[0]
    _with_history(state, first)

    def fork():
        return asyncio.run(
            sc.fork_session(state, caller_session_key=slot_history_key(caller), source=first)
        )

    if on:
        before = set(state._slots)
        _refused(fork)
        assert set(state._slots) == before
    else:
        assert fork()["target"] in state._slots


@pytest.mark.parametrize("on", [True, False])
def test_a_revive_that_would_open_the_fifth(crewmate, tmp_path, monkeypatch, on):  # noqa: F811
    state, caller, _execution = crewmate
    # The crew log's creation lineage, which a fenced reviver needs, is off here.
    monkeypatch.setattr(sc, "_creation_parent", lambda key: caller.key)
    _bot(tmp_path, worker_conducts=on)
    key = _create(state, caller)["target"]
    _with_history(state, key)
    state.get_slot(key)._dirty = True
    asyncio.run(sc.close_target(state, caller_session_key=slot_history_key(caller), target=key))
    assert key not in state._slots
    meta = state.conversation_log.get_metadata(f"dashboard:{key}")
    # The close is strictly before the revive (the revive helpers explain why).
    state.conversation_log.update_metadata(
        f"dashboard:{key}", {"closed_at": float(meta["closed_at"]) - 60.0}
    )
    _open_workers(state, caller, FOUR)

    def revive():
        return asyncio.run(
            sc.revive_session(state, caller_session_key=slot_history_key(caller), target=key)
        )

    if on:
        _refused(revive)
        assert key not in state._slots
    else:
        revive()
        assert key in state._slots


@pytest.mark.parametrize("cap", [2, 6])
def test_a_limit_the_owner_set_is_used_as_is(crewmate, tmp_path, cap):  # noqa: F811
    """Below four or above it: the owner's number, never the default bound."""
    state, caller, _execution = crewmate
    _bot(tmp_path, worker_conducts=True, max_workers=cap)
    _open_workers(state, caller, cap)
    refusal = _refused(lambda: _create(state, caller))
    assert f"allows {cap}" in str(refusal)


def test_an_unreadable_switch_leaves_todays_check(crewmate, tmp_path, monkeypatch):  # noqa: F811
    """Read as ``bot_max_workers`` reads the limit: unreadable never refuses."""
    from kiro_crew import crewmate_harness
    from kiro_crew.execution_context import read_session_execution
    from kiro_crew.mate_worker_conducts import effective_worker_cap_refusal

    state, caller, _execution = crewmate
    _bot(tmp_path, worker_conducts=True)
    keys = _open_workers(state, caller, FOUR)
    execution = read_session_execution(slot_history_key(state.get_slot(keys[0])), required=True)
    assert effective_worker_cap_refusal(state, execution) is not None

    def unreadable(*_args, **_kwargs):
        raise OSError("config unreadable")

    monkeypatch.setattr(crewmate_harness, "crew_for_member_id", unreadable)
    assert effective_worker_cap_refusal(state, execution) is None


def test_the_texts_that_name_the_limit_name_the_constant():
    """The settings hints an owner reads quote the number the gateway applies."""
    from kiro_crew.config.sections_session import BOT_MAX_WORKERS_CONDUCTING

    help_of = {f.name: f.metadata["help"] for f in dataclasses.fields(KiroCrewAgentConfig)}
    assert f"0 means at most {BOT_MAX_WORKERS_CONDUCTING}." in help_of["max_workers"]
    assert f"at most {BOT_MAX_WORKERS_CONDUCTING} of the Bot's workers" in (
        help_of["worker_conducts"]
    )
