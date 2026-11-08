"""Structured asks in the warm task brief (RFC section 3.9.3): each ask says
why it is asked, what each option does and which one is recommended, and the
option labels are only ever the source entry's own."""

from __future__ import annotations

import json

from kiro_crew.dashboard import warm_task_brief

WAITING = (
    {"kind": "options", "text": "Push now or wait?", "options": ["Push now", "Wait for CI"]},
    {"kind": "card", "text": "Approve the change", "options": []},
)


def _ask(**fields):
    return {
        "what": "Push the fix",
        "why": "Only you can approve a push",
        "source": 0,
        "options": [
            {"label": "Push now", "consequence": "CI reruns, about 30 checks"},
            {"label": "Wait for CI", "consequence": "Nothing moves until it ends"},
        ],
        "recommended": "Push now",
        "recommended_why": "The fix is tested",
        **fields,
    }


def _parse(*asks, **fields):
    raw = json.dumps({"headline": "Back.", "asks": list(asks), **fields})
    return warm_task_brief._parse_result(raw, "Fix the nightly", (), WAITING)


def test_an_ask_keeps_why_each_consequence_and_the_recommendation():
    brief = _parse(_ask())
    assert brief is not None
    (ask,) = brief.asks
    assert ask.why == "Only you can approve a push"
    assert ask.options == (
        ("Push now", "CI reruns, about 30 checks"),
        ("Wait for CI", "Nothing moves until it ends"),
    )
    assert (ask.recommended, ask.recommended_why) == ("Push now", "The fix is tested")
    # Something waits on the owner, so the chip cannot read "on track".
    assert brief.status == "needs_you"
    assert brief.as_meta()["asks"][0]["options"][0] == {
        "label": "Push now",
        "consequence": "CI reruns, about 30 checks",
    }
    text = warm_task_brief._brief_text(brief, "en")
    assert "Needs you:\n1. Push the fix\n   Why: Only you can approve a push\n" in text
    assert "   - Wait for CI: Nothing moves until it ends\n" in text
    assert "   Recommended: Push now (The fix is tested)" in text


def test_renamed_or_added_labels_keep_the_source_labels_and_drop_the_rest():
    renamed = _ask(
        options=[
            {"label": "Ship it", "consequence": "x"},
            {"label": "Wait for CI", "consequence": "y"},
        ]
    )
    added = _ask(options=[*_ask()["options"], {"label": "Close it", "consequence": "z"}])
    for raw in (renamed, added):
        brief = _parse(raw)
        assert brief is not None
        (ask,) = brief.asks
        assert ask.options == (("Push now", ""), ("Wait for CI", ""))
        assert ask.recommended == "" and ask.recommended_why == ""


def test_an_ask_without_a_valid_source_has_no_options():
    for source in (None, 1, 7, -1, True, "0"):
        brief = _parse(_ask(source=source))
        assert brief is not None
        (ask,) = brief.asks
        assert ask.what == "Push the fix" and ask.options == () and ask.recommended == ""


def test_a_recommendation_outside_the_labels_is_dropped():
    brief = _parse(_ask(recommended="Merge it"))
    assert brief is not None
    (ask,) = brief.asks
    assert ask.options[0] == ("Push now", "CI reruns, about 30 checks")
    assert ask.recommended == "" and ask.recommended_why == ""


def test_malformed_asks_are_dropped_and_the_list_is_capped():
    junk = ["text", {"why": "no what"}, {"what": "x", "options": 5}]
    brief = _parse(*junk, *[_ask(what=f"Ask {n}") for n in range(6)])
    assert brief is not None
    assert [ask.what for ask in brief.asks][0] == "x"
    assert len(brief.asks) == warm_task_brief._MAX_ITEMS


def test_asks_replace_the_older_needs_you_lines():
    brief = _parse(_ask(), needs_you=["Push the fix"])
    assert brief is not None and brief.needs_you == ()
    older = _parse(needs_you=["Push the fix"])
    assert older is not None and older.needs_you == ("Push the fix",) and older.asks == ()


def test_ask_text_is_cleaned_and_scanned_like_every_model_field():
    brief = _parse(_ask(why="see [OPTIONS: Go | Stop] now"))
    assert brief is not None
    assert "[OPTIONS" not in brief.asks[0].why
    # The output check reads ask text too: a SHA the input never gave drops it.
    bad = _parse(_ask(recommended_why="matches deadbeef123"))
    assert bad is not None and "deadbeef123" in bad.model_text()
    assert warm_task_brief._unbacked_facts(bad.model_text(), "", set()) == ["deadbeef123"]
    assert warm_task_brief._gateway_only(bad, "en").asks == ()


def test_ask_text_in_content_carries_no_live_widget_link_or_address():
    widget = '<mcwidget><script src="https://cdn.jsdelivr.net/npm/d3@7"></script></mcwidget>'
    brief = _parse(
        _ask(
            what=widget,
            why="see [the PR](https://evil.example/x)",
            recommended_why="go to https://evil.example/y",
        )
    )
    assert brief is not None
    text = warm_task_brief._brief_text(brief, "en")
    assert "mcwidget" not in text.lower() and "cdn.jsdelivr" not in text
    assert "](" not in text and "`https://evil.example/y`" in text
    anchor = _parse(_ask(why='<a href="//example.org/approval">Review here</a>'))
    assert anchor is not None
    shown = warm_task_brief._brief_text(anchor, "en")
    assert "<a" not in shown and "&lt;a href=" in shown


def test_the_welcome_restates_the_request_from_the_model_or_the_quote():
    # The model's restatement wins, beside a body too.
    restated = _parse(asked="Get the nightly green", body="- one fix in")
    assert restated.asked == "Get the nightly green"
    # The quote fills in only without a body.
    assert _parse().asked == "Fix the nightly"
    assert _parse(body="- one fix in").asked == ""


def test_a_brief_with_every_field_at_its_cap_fits_the_output_budget():
    wtb = warm_task_brief
    han = "界"

    def full(cap):
        return '"' + han * (cap - 1)

    ask = {
        "what": full(wtb._MAX_ITEM_CHARS),
        "why": full(wtb._MAX_ITEM_CHARS),
        "source": 0,
        "options": [
            {"label": full(80), "consequence": full(wtb._MAX_CONSEQUENCE_CHARS)}
            for _ in range(wtb._MAX_ASK_OPTIONS)
        ],
        "recommended": full(80),
        "recommended_why": full(wtb._MAX_ITEM_CHARS),
    }
    raw = {
        "status": "needs_you",
        "headline": full(wtb._MAX_HEADLINE_CHARS),
        "asked": full(wtb._MAX_ASKED_CHARS),
        "asks": [ask] * wtb._MAX_ITEMS,
        "body": '<mcwidget style="x">' + '"' * 200 + han * (wtb._MAX_BODY_CHARS - 240),
    }
    size = len(json.dumps(raw, ensure_ascii=False, indent=2).encode("utf-8"))
    assert size <= wtb.WARM_TASK_BRIEF_MAX_OUTPUT_BYTES


def test_the_prompt_asks_for_structured_asks_and_gives_the_request():
    snapshot = warm_task_brief.TaskSnapshot(
        slot_key="s",
        history_key="h",
        original_request="Make the nightly green",
        fingerprint="f",
        session_state={},
        conductor=None,
        work_items=(),
        previous_claim="",
        waiting=WAITING,
    )
    prompt = warm_task_brief._build_prompt(snapshot)
    for field in ('"asks"', '"source"', '"consequence"', '"recommended"', '"why"'):
        assert field in prompt
    assert '"owner_request": "Make the nightly green"' in prompt
    assert '"needs_you": [' not in prompt
