"""``session.automated_idle_timeout_secs``: a session automation woke expires sooner.

The release that ends a turn records whether its entry point declared a starter other than a
person; the idle sweep then holds that session for the shorter timeout. A person's turn, or a
context that declared no starter, keeps ``session.timeout_secs``. The shorter value never
lengthens a hold, and 0 turns it off.
"""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import _build_session_config
from kiro_crew.config.sections_session import SessionConfig
from kiro_crew.metrics.usage_attrs import (
    _CURRENT_TRIGGER,
    TRIGGER_CRON,
    TRIGGER_MONITOR,
    TRIGGER_USER,
    set_current_trigger,
)
from kiro_crew.session import SessionManager
from kiro_crew.session_idle_clock import idle_limit, turn_was_automated

AUTOMATED_KEY = "dashboard:chat-automated"
PERSON_KEY = "dashboard:chat-person"
UNDECLARED_KEY = "dashboard:chat-undeclared"
RECENT_AUTOMATED_KEY = "dashboard:chat-automated-recent"
TIMEOUT = 10_000
AUTOMATED_TIMEOUT = 60
# Past the automated timeout and its one passive window, well inside the ordinary one.
IDLE_FOR = 1_000


def _provider_factory():
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        provider = AsyncMock()
        provider.context_usage_pct = lambda: 0.0
        provider.has_active_turn = lambda: False
        return provider

    return factory


def _manager(automated_timeout_secs: int) -> SessionManager:
    cfg = KiroCrewConfig()
    cfg.session.automated_idle_timeout_secs = automated_timeout_secs
    return SessionManager(cfg, provider_factory=_provider_factory())


async def _turn(manager: SessionManager, key: str, trigger: str | None) -> None:
    """One turn on *key* whose entry point declared *trigger* (None: declared nothing)."""
    previous = _CURRENT_TRIGGER.get()
    _CURRENT_TRIGGER.set(trigger)
    try:
        await manager.get_or_create(key)
        manager.release(key)
    finally:
        _CURRENT_TRIGGER.set(previous)


async def _age(manager: SessionManager, key: str) -> None:
    async with manager._lock:
        manager._sessions[key].last_used = time.monotonic() - IDLE_FOR


@pytest.mark.asyncio
async def test_a_session_automation_woke_expires_on_the_shorter_timeout() -> None:
    manager = _manager(AUTOMATED_TIMEOUT)
    await _turn(manager, AUTOMATED_KEY, TRIGGER_CRON)
    await _turn(manager, PERSON_KEY, TRIGGER_USER)
    await _turn(manager, UNDECLARED_KEY, None)
    for key in (AUTOMATED_KEY, PERSON_KEY, UNDECLARED_KEY):
        await _age(manager, key)

    await manager._expire_idle(TIMEOUT)

    assert AUTOMATED_KEY not in manager._sessions, "an automated wake kept the full idle hold"
    assert PERSON_KEY in manager._sessions, "a person's session must keep session.timeout_secs"
    assert UNDECLARED_KEY in manager._sessions, "an undeclared starter must not count as automated"
    await manager.close_all()


@pytest.mark.asyncio
async def test_a_persons_turn_puts_the_session_back_on_the_ordinary_timeout() -> None:
    manager = _manager(AUTOMATED_TIMEOUT)
    await _turn(manager, AUTOMATED_KEY, TRIGGER_MONITOR)
    assert manager._sessions[AUTOMATED_KEY].last_turn_automated is True

    await _turn(manager, AUTOMATED_KEY, TRIGGER_USER)
    await _age(manager, AUTOMATED_KEY)
    await manager._expire_idle(TIMEOUT)

    assert AUTOMATED_KEY in manager._sessions
    assert manager._sessions[AUTOMATED_KEY].last_turn_automated is False
    await manager.close_all()


@pytest.mark.asyncio
async def test_zero_leaves_an_automated_session_on_the_ordinary_timeout() -> None:
    manager = _manager(0)
    await _turn(manager, AUTOMATED_KEY, TRIGGER_CRON)
    await _age(manager, AUTOMATED_KEY)

    await manager._expire_idle(TIMEOUT)

    assert AUTOMATED_KEY in manager._sessions
    await manager.close_all()


def test_the_limit_is_never_longer_than_the_ordinary_timeout() -> None:
    automated = SimpleNamespace(last_turn_automated=True)
    person = SimpleNamespace(last_turn_automated=False)

    assert idle_limit(automated, 600, 60) == 60
    assert idle_limit(automated, 600, 6_000) == 600
    assert idle_limit(automated, 600, 0) == 600
    assert idle_limit(person, 600, 60) == 600
    assert idle_limit(object(), 600, 60) == 600


def test_only_a_declared_starter_other_than_a_person_is_automated() -> None:
    previous = _CURRENT_TRIGGER.get()
    _CURRENT_TRIGGER.set(None)
    try:
        assert turn_was_automated() is False
        set_current_trigger(TRIGGER_USER)
        assert turn_was_automated() is False
        set_current_trigger(TRIGGER_CRON)
        assert turn_was_automated() is True
    finally:
        _CURRENT_TRIGGER.set(previous)


def test_the_loader_reads_and_clamps_the_value() -> None:
    assert _build_session_config({}).automated_idle_timeout_secs == 0
    assert (
        _build_session_config({"automated_idle_timeout_secs": 900}).automated_idle_timeout_secs
        == 900
    )
    assert (
        _build_session_config({"automated_idle_timeout_secs": -5}).automated_idle_timeout_secs == 0
    )
    assert (
        _build_session_config({"automated_idle_timeout_secs": 10**9}).automated_idle_timeout_secs
        == 86_400
    )


@pytest.mark.asyncio
async def test_a_longer_value_leaves_an_automated_session_on_the_ordinary_timeout() -> None:
    manager = _manager(TIMEOUT * 2)
    await _turn(manager, AUTOMATED_KEY, TRIGGER_CRON)
    async with manager._lock:
        manager._sessions[AUTOMATED_KEY].last_used = time.monotonic() - (TIMEOUT + IDLE_FOR)
    await _turn(manager, RECENT_AUTOMATED_KEY, TRIGGER_CRON)
    await _age(manager, RECENT_AUTOMATED_KEY)

    await manager._expire_idle(TIMEOUT)

    assert AUTOMATED_KEY not in manager._sessions, "a longer value must not lengthen the hold"
    assert RECENT_AUTOMATED_KEY in manager._sessions, "inside the ordinary timeout it stays"
    await manager.close_all()


@pytest.mark.asyncio
async def test_with_the_ordinary_timeout_at_zero_the_value_has_no_effect() -> None:
    manager = _manager(AUTOMATED_TIMEOUT)
    manager._cfg.session.timeout_secs = 0
    await _turn(manager, AUTOMATED_KEY, TRIGGER_CRON)

    manager._cleanup_boundary()._adopt_idle_policy()

    assert manager._cleanup_boundary().state.idle_sweep_enabled is False
    assert idle_limit(manager._sessions[AUTOMATED_KEY], 0, AUTOMATED_TIMEOUT) == 0
    await manager.close_all()


# What each copy states about the two relationships the tests above prove: the value is used
# only when it is shorter than the ordinary timeout, and not while that timeout is 0.
_REPO = Path(__file__).resolve().parents[1]


def _help_text() -> str:
    fields = {f.name: f for f in dataclasses.fields(SessionConfig)}
    return fields["automated_idle_timeout_secs"].metadata["help"]


def _configuration_row() -> str:
    text = (_REPO / "src/kiro_crew/docs/configuration.md").read_text(encoding="utf-8")
    return next(
        line
        for line in text.splitlines()
        if line.startswith("| `session.automated_idle_timeout_secs`")
    )


def _session_spec_passage() -> str:
    text = (_REPO / "docs/system-specs/modules/session.md").read_text(encoding="utf-8")
    start = text.index("`session.automated_idle_timeout_secs` (default 0")
    return text[start : text.index("A key listed in `session.idle_exempt_keys`", start)]


def _config_spec_line() -> str:
    text = (_REPO / "docs/system-specs/modules/config.md").read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if "automated_idle_timeout_secs: int" in line)


def _baseline_help() -> str:
    entries = json.loads((_REPO / "config-baseline.json").read_text(encoding="utf-8"))["entries"]
    return next(e["help"] for e in entries if e["path"] == "session.automated_idle_timeout_secs")


def _states(copy: str, *phrases: str) -> bool:
    flat = " ".join(copy.split())
    return all(phrase in flat for phrase in phrases)


_COPIES = {
    "help": (
        _help_text,
        ("only when shorter than the Session Timeout", "not while that is 0"),
    ),
    "config-baseline.json": (
        _baseline_help,
        ("only when shorter than the Session Timeout", "not while that is 0"),
    ),
    "configuration.md": (
        _configuration_row,
        ("only when it is shorter than `session.timeout_secs`", "not while that is `0`"),
    ),
    "session.md": (
        _session_spec_passage,
        (
            "only when it is shorter than `session.timeout_secs`",
            "with `session.timeout_secs` at 0 the sweep does not run, so it has no effect",
        ),
    ),
    "config.md": (_config_spec_line, ("used only when shorter than timeout_secs",)),
}


@pytest.mark.parametrize("name", sorted(_COPIES))
def test_each_copy_states_the_relationships_the_sweep_applies(name: str) -> None:
    read, phrases = _COPIES[name]
    copy = read()

    assert _states(copy, *phrases), f"{name} no longer states what the sweep applies"
    falsified = copy.replace("shorter", "longer").replace("not while", "even while")
    assert not _states(falsified, *phrases), f"{name}'s check accepts the opposite claim"


# Spellings of the opposite relationships: a longer value lengthening the hold, or the value
# applying while the ordinary timeout is 0. None may sit beside the correct wording.
_OPPOSITE_CLAIMS = (
    "longer than",
    "lengthens",
    "even while",
    "even when",
    "also while",
    "while that is 0 it",
    "at 0 it applies",
    "at 0 the sweep still",
)


@pytest.mark.parametrize("name", sorted(_COPIES))
def test_no_copy_states_the_opposite_relationship(name: str) -> None:
    read, _phrases = _COPIES[name]
    flat = " ".join(read().split()).lower()

    found = [claim for claim in _OPPOSITE_CLAIMS if claim in flat]
    assert not found, f"{name} states the opposite of what the sweep applies: {found}"
