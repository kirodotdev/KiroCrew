"""The per-channel policy ceiling (``allowed_channel_ids``) on code-channel dispatch.

Every inbound Slack path, every write and the Archive button apply
``channel_policy.channel_refusal``. The dispatch exemption a code channel's own
session conducts through applies it too: with an explicit-id allowlist that leaves
the channel out, the session creates no worker and opens no ledger, and the policy
seam audits the refusal. Restart resume and the notice buttons are covered beside
their own tests (``test_slack_code_channel_resume.py``,
``test_slack_code_channel_handoff.py``).
"""

from __future__ import annotations

import pytest
from slack_code_channel_policy_helpers import allow_only, policy_refusals
from test_session_control_owner_dm import _state
from test_slack_code_channel_dispatch_exemption import (  # noqa: F401 - fixtures
    CHANNEL,
    _channel,
    _create,
    _enabled,
    _owner_turn,
    _record,
    _refused_create,
    ledger_routes_open,
)

from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.dashboard.session_control_parts import code_channel_exemption as cce

OPERATION = "slack.code_channel.dispatch"


def test_a_channel_the_allowlist_leaves_out_creates_no_worker(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    before = set(state._slots)
    rows = allow_only(monkeypatch, "C0OTHERCHAN")
    exc = _refused_create(state, slot)
    assert exc.code == "mirrored_caller"
    assert "security policy does not admit the code channel" in exc.message
    assert set(state._slots) == before, "no worker was created"
    # Recorded once, by the judgement's own off-loop audit, not inline on the loop.
    assert policy_refusals(rows, OPERATION) == []


def test_a_channel_the_allowlist_leaves_out_opens_no_ledger(
    tmp_path, monkeypatch, ledger_routes_open  # noqa: F811
):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    allow_only(monkeypatch, "C0OTHERCHAN")
    status, _body = _record(state, slot_history_key(slot), {"action": "goal", "goal": "g"})
    assert status != 200
    assert wl.read_conductor(routes.session_ledger.ledger_key(slot_history_key(slot))) is None


def test_the_allowlist_naming_the_channel_admits_it(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    slot, _ = _channel(state, tmp_path)
    _owner_turn(slot)
    allow_only(monkeypatch, CHANNEL)
    assert state.get_slot(_create(state, slot)["target"]) is not None


def test_the_channel_state_check_names_the_policy(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    _slot, store = _channel(state, tmp_path)
    allow_only(monkeypatch, "C0OTHERCHAN")
    binding = store.get_binding(CHANNEL)
    assert cce._channel_state_refusal(binding) == (
        "the security policy does not admit the code channel"
    )


@pytest.mark.parametrize("pattern", ["C0OTHER*", "D*"])
def test_a_glob_that_does_not_match_the_channel_still_refuses(tmp_path, monkeypatch, pattern):
    state = _state(tmp_path, monkeypatch)
    _slot, store = _channel(state, tmp_path)
    allow_only(monkeypatch, pattern)
    assert cce._channel_state_refusal(store.get_binding(CHANNEL))
