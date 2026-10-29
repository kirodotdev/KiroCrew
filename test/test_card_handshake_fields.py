"""The card given a session's ``initialize`` ANDs the field the runtime gates on.

``card_for(backend)`` is membership alone. ``card_for(backend, init)`` also needs the
handshake field for each (harness, line) in ``ADVERTISED_BY_HARNESS``: today the
``_session/steering`` advertisement (``_meta.steering.supported``) for claude and
codex. Every init here is replayed from a committed live capture.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterator, Tuple

import pytest

from kiro_crew.acp.client_turn_control import steer_unadvertised_issue
from kiro_crew.acp.types import steering_advertised
from kiro_crew.agent_sdk import backends
from kiro_crew.agent_sdk.backend_cards import (
    ADVERTISED_BY_HARNESS,
    ADVERTISED_FIELD_BY_LINE,
    LINE_MID_TURN_STEER,
    USER_FACING_LINES,
    card_for,
)
from kiro_crew.providers.acp import AcpProvider

FRAMES = Path(__file__).resolve().parent / "fixtures" / "acp_frames"
CLAUDE = backends.ACP_BACKEND_CLAUDE
CODEX = backends.ACP_BACKEND_CODEX


def _live_inits() -> Iterator[Tuple[str, str, dict]]:
    """(backend, capture name, initialize result) for every live capture with one."""
    for path in sorted(FRAMES.glob("*/*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines()
        header = json.loads(lines[0]).get("_meta", {}) if lines else {}
        if header.get("recorded") != "live":
            continue
        for line in lines[1:]:
            frame = json.loads(line) if line.strip() else {}
            result = frame.get("result") if isinstance(frame, dict) else None
            if isinstance(result, dict) and "protocolVersion" in result:
                yield header["backend"], f"{path.parent.name}/{path.name}", result
                break


LIVE_INITS = list(_live_inits())


def live_init(capture: str) -> dict:
    """A deep copy of *capture*'s live ``initialize`` result."""
    return copy.deepcopy(next(init for _, name, init in LIVE_INITS if name == capture))


def _without_steering(init: dict) -> dict:
    dropped = copy.deepcopy(init)
    dropped["_meta"].pop("steering")
    return dropped


def _cells(card) -> Dict[str, bool]:
    return {line.id: line.available for line in (*card.capabilities, *card.details)}


def test_live_inits_cover_every_gated_harness():
    seen = {backend for backend, _, _ in LIVE_INITS}
    gated = {getattr(backends, name) for name, _ in ADVERTISED_BY_HARNESS}
    assert gated <= seen


@pytest.mark.parametrize(
    ("backend", "capture", "init"), LIVE_INITS, ids=[n for _, n, _ in LIVE_INITS]
)
def test_no_cell_flips_for_a_current_adapter(backend, capture, init):
    """Before/after: the session card equals the membership card on every live capture."""
    assert card_for(backend, init=init) == card_for(backend)


@pytest.mark.parametrize(
    ("backend", "capture"),
    [(CODEX, "codex/steering-live.jsonl"), (CLAUDE, "claude/session.jsonl")],
)
def test_an_adapter_that_drops_the_field_loses_the_steer_line(backend, capture):
    before = _cells(card_for(backend, init=live_init(capture)))
    after = _cells(card_for(backend, init=_without_steering(live_init(capture))))
    assert before[LINE_MID_TURN_STEER] is True
    assert after[LINE_MID_TURN_STEER] is False
    assert {k: v for k, v in before.items() if k != LINE_MID_TURN_STEER} == {
        k: v for k, v in after.items() if k != LINE_MID_TURN_STEER
    }
    line = next(
        x for x in card_for(backend, init={"x": 1}).capabilities if x.id == LINE_MID_TURN_STEER
    )
    assert line.measured is True and line.unmeasured_reason == ""


@pytest.mark.parametrize("value", [False, "true", 1, None, {}])
def test_only_a_literal_true_keeps_the_line(value):
    init = live_init("codex/steering-live.jsonl")
    init["_meta"]["steering"]["supported"] = value
    assert _cells(card_for(CODEX, init=init))[LINE_MID_TURN_STEER] is False
    assert steering_advertised(init) is False


@pytest.mark.parametrize("backend", sorted(set(backends.ACP_BACKENDS_KNOWN) - {CLAUDE, CODEX}))
def test_a_harness_outside_the_registry_ignores_the_init(backend):
    """kiro-cli, KAS, goose, pi, opencode and the rest steer on a verb that
    advertises nothing, so an init without the field changes nothing."""
    assert card_for(backend, init={"protocolVersion": 1}) == card_for(backend)


def test_every_gate_reads_an_initialize_field_on_a_core_line():
    core = {spec.id for spec in USER_FACING_LINES}
    for _, line_id in ADVERTISED_BY_HARNESS:
        assert line_id in core
        assert ADVERTISED_FIELD_BY_LINE[line_id].startswith("initialize.")


# ── The consumer: the new-chat notice reads the session card ──


def _notice(backend: str, init) -> str:
    inner = SimpleNamespace(backend=backend, agent_version="", initialize_result=init)
    return AcpProvider.steer_upgrade_notice.fget(SimpleNamespace(_client=inner, agent_version=""))


@pytest.mark.parametrize(
    ("backend", "capture"),
    [(CODEX, "codex/steering-live.jsonl"), (CLAUDE, "claude/session.jsonl")],
)
def test_the_notice_fires_only_when_the_handshake_drops_the_field(backend, capture):
    assert _notice(backend, live_init(capture)) == ""
    notice = _notice(backend, _without_steering(live_init(capture)))
    assert "waits until it finishes" in notice
    assert backend in notice


@pytest.mark.parametrize("init", [None, {}])
def test_no_notice_before_the_handshake(init):
    assert _notice(CODEX, init) == ""
    assert steer_unadvertised_issue(CLAUDE, init) == ""


@pytest.mark.parametrize(
    ("backend", "capture", "init"), LIVE_INITS, ids=[n for _, n, _ in LIVE_INITS]
)
def test_no_live_capture_gets_the_notice(backend, capture, init):
    assert steer_unadvertised_issue(backend, init) == ""
