"""Scheduled attribution agrees across cron tabs, catalog rows and stored prompts."""

from __future__ import annotations

import pytest
from chat_test_helpers import _make_state

from kiro_crew.cron import CronJob
from kiro_crew.dashboard.cron_inject import (
    _MIN_PROMPT_CHARS_TO_REFERENCE,
    _REFERENCE_MARKER,
    hydrate_slot_from_history,
    inject_cron_result_to_dashboard,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.shared_crew import attribution, trigger
from kiro_crew.shared_crew.admission_gate import AUTOMATION_PRINCIPAL
from kiro_crew.shared_crew.contracts import ActorContext, Principal


@pytest.fixture
def named_schedule(monkeypatch):
    monkeypatch.setattr(
        trigger, "_namer", lambda ref: "nightly-digest" if ref == "abc12345" else ""
    )


@pytest.mark.usefixtures("named_schedule")
@pytest.mark.parametrize(
    "key",
    ["cron-abc12345", "dashboard:cron-abc12345", "dashboard_cron-abc12345"],
)
def test_cron_tab_and_catalog_name_the_schedule(key):
    slot = _ChatSlot(key)
    slot._origin = "cron"
    expected = {
        "kind": "scheduled",
        "ref": "abc12345",
        "name": "nightly-digest",
    }
    assert attribution.public_attribution_of_slot(slot).get("trigger") == expected
    assert attribution.public_attribution_from_meta({}, key=key).get("trigger") == expected
    assert slot.to_dict()["attribution"]["trigger"] == expected


def test_cron_origin_without_a_job_ref_still_names_a_schedule():
    slot = _ChatSlot("retained-tab")
    slot._origin = "cron"
    assert attribution.public_attribution_of_slot(slot).get("trigger") == {
        "kind": "scheduled",
        "ref": "",
        "name": "",
    }


@pytest.mark.parametrize("key", ["cron-not-a-job", "cron-abc12345junk", "personal"])
def test_non_cron_keys_do_not_invent_a_trigger(key):
    assert trigger.trigger_of(key=key) is None


def test_human_creator_is_not_replaced_by_a_cron_label():
    from kiro_crew.shared_crew.contracts import Transport

    slot = _ChatSlot("cron-abc12345")
    human = Principal.from_dict({"id": "p_human", "kind": "human"})
    actor = ActorContext(principal=human, request_id="request", transport=Transport.HTTP)
    attribution.stamp_creator(slot, actor, now=1.0)
    assert "trigger" not in attribution.public_attribution_of_slot(slot)


@pytest.mark.parametrize("long_prompt", [False, True])
def test_cron_prompt_author_survives_durable_write_and_hydration(tmp_path, long_prompt):
    state = _make_state(tmp_path)
    job = CronJob(
        id="abc12345",
        name="nightly-digest",
        message="x" * (_MIN_PROMPT_CHARS_TO_REFERENCE + 1) if long_prompt else "x",
    )
    for stamp in (1.0, 2.0):
        job.last_result_ts = stamp
        inject_cron_result_to_dashboard(state, job, "result", history=[], dismissed=[])
    slot = state.get_slot("cron-abc12345")
    disk = state.conversation_log.read_messages("cron:abc12345")
    restored = _ChatSlot("restored")
    hydrate_slot_from_history(restored, disk)
    for rows in (slot.messages, disk, restored.messages):
        prompts = [row for row in rows if row["role"] == "user"]
        assert len(prompts) == 2
        for prompt in prompts:
            assert prompt["meta"]["author"] == AUTOMATION_PRINCIPAL.to_public_dict()
        assert (_REFERENCE_MARKER in prompts[1]["content"]) is long_prompt
    inject_cron_result_to_dashboard(state, job, "result", history=None, dismissed=[])
    assert len(slot.messages) == len(disk) == 4
