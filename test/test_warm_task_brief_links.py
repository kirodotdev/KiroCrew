"""The warm welcome's link row (RFC ``rfc-delayed-task-brief.md`` section 3.9.3).

The gateway adds one link per pull request it read live, labelled
``owner/repo#N`` with the URL built from that read's own repo. It never
rewrites the model's text, and the section 3.9.6 checks read the posted text
with the row in it.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from test_warm_task_brief import _arm_model, _claimed, _foreground_slot, _snapshot, _State

from kiro_crew.dashboard import warm_task_brief
from kiro_crew.dashboard import warm_task_brief_inputs as inputs

_OURS = inputs.PrRef("kirodotdev/KiroCrew", 12)
_THEIRS = inputs.PrRef("acme/other", 12)
_OPEN = {"state": "open", "review": "", "mergeable": "MERGEABLE", "checks": None}


def _brief(body: str = "#12 is green.") -> warm_task_brief.Brief:
    return warm_task_brief.Brief(
        status="on_track",
        headline="Back.",
        asked="",
        remaining="",
        done=(),
        needs_you=(),
        next=(),
        extra=(),
        body=body,
    )


def _entry(ref: inputs.PrRef, checked: bool = True) -> dict:
    return {"pr": f"#{ref.number}", "repo": ref.repo, "url": ref.url, "checked": checked}


def test_only_prs_read_live_get_a_link_once_each():
    live = (_entry(_OURS), _entry(_OURS), _entry(inputs.PrRef("kirodotdev/KiroCrew", 13), False))
    assert warm_task_brief._live_links(live) == (_OURS,)


def test_the_same_number_in_two_repos_gets_two_links_each_labelled_with_its_repo():
    brief = replace(_brief(), links=warm_task_brief._live_links((_entry(_OURS), _entry(_THEIRS))))
    assert brief.as_meta()["links"] == [_OURS.url, _THEIRS.url]
    assert warm_task_brief._brief_text(brief, "en").endswith(
        "kirodotdev/KiroCrew#12 · acme/other#12"
    )


def test_no_row_without_a_live_read():
    brief = replace(_brief(), links=warm_task_brief._live_links(()))
    assert "links" not in brief.as_meta()
    assert warm_task_brief._brief_text(brief, "en") == "On track: Back.\n#12 is green."


def test_the_secret_check_reads_the_posted_text_with_the_row(monkeypatch):
    brief = replace(_brief(), links=(_OURS,))
    seen: list[str] = []
    monkeypatch.setattr(warm_task_brief, "_holds_a_secret", lambda text: seen.append(text))
    content = warm_task_brief._brief_text(brief, "en")
    assert warm_task_brief._grounding_miss(brief, content, {12}, "", set()) == ""
    assert "kirodotdev/KiroCrew#12" in seen[0] and _OURS.url in seen[0]
    # A miss drops the row with the model's fields.
    assert warm_task_brief._gateway_only(brief, "en").links == ()


@pytest.mark.asyncio
async def test_a_posted_brief_keeps_the_body_as_written_and_links_only_the_live_read(monkeypatch):
    monkeypatch.setattr(inputs, "_live_cache", {})
    monkeypatch.setattr(
        inputs, "probe_github", lambda urls: {u: _OPEN for u in urls if u == _OURS.url}
    )
    state = _State()
    slot = _foreground_slot(state)
    base = replace(
        _snapshot(),
        waiting=(
            {
                "kind": "options",
                "text": f"Merge {_OURS.url}? Also see {_THEIRS.url}.",
                "options": ["Merge it", "Hold"],
                "asked_at": "",
            },
        ),
    )
    body = "#12 is mergeable.\n\n```\nPR #12\n```"

    async def run_model(*args, **kwargs):
        return json.dumps({"headline": "Back.", "body": body})

    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    row = slot.rows[-1]
    assert row["meta"]["brief"]["body"] == body
    # acme/other#12 was named but its read failed: no link.
    assert row["meta"]["brief"]["links"] == [_OURS.url]
    assert row["content"].endswith("kirodotdev/KiroCrew#12")
