"""The gateway-written warm welcome (RFC ``rfc-delayed-task-brief.md`` 3.11).

Pins the four parts and their order, the ask record shown as written and the plain
line when none was, PR state only from this welcome's read, "checked HH:MM", the
cleaning every ledger string gets, the ``objects`` fold and its ``warm_brief``
producer, and the Board's ``pr_facts`` reader.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.crew_log import projection_objects as objects
from kiro_crew.dashboard import warm_task_brief
from kiro_crew.dashboard import warm_task_brief_inputs as inputs
from kiro_crew.dashboard import warm_task_brief_welcome as welcome
from kiro_crew.dashboard.warm_task_brief import work_item_is_terminal

URL = "https://github.com/acme/widgets/pull/{n}"
RECORD = {
    "what": "Merge PR 7?",
    "why": "Only you can merge.",
    "options": [{"label": "Merge", "outcome": "ships today"}, {"label": "Hold", "outcome": ""}],
    "pick": "Merge",
    "pick_why": "all checks pass",
    "written_at": "2026-11-09T05:30:00+00:00",
}


def _item(n, *, status="progress", worker=True, pr=None, ask=None, state="open"):
    item = {
        "item_id": f"it_{n:08x}",
        "title": f"Item {n}",
        "state": state,
        "status": status,
        "worker_session_key": f"w{n}" if worker else None,
    }
    if pr is not None:
        item["acceptance"] = {"kind": "pr_checks", "repo": "acme/widgets", "pr": pr}
    if ask is not None:
        item["ask"] = ask
    return item


def _live(n, **facts):
    return {"url": URL.format(n=n), "pr": f"#{n}", "repo": "acme/widgets", "checked": True,
            "read_at": "06:00", **facts}  # fmt: skip


def _render(items, *, awaits=(), live=(), waiting=(), language="en", goal="Ship the widget"):
    return welcome.render(
        language=language,
        goal=goal,
        items=items,
        awaits={i["item_id"]: i["item_id"] in awaits for i in items},
        waiting=waiting,
        live=live,
        checked_at="06:01",
        clean=warm_task_brief._clean,
        terminal=work_item_is_terminal,
    )


def test_nothing_waiting_is_one_plain_line_and_the_parts_keep_their_order():
    live = [_live(7, state="open")]
    text = _render([_item(1, pr=7), _item(2, worker=False, status=None)], live=live)
    assert text.split("\n") == [
        "**Goal**: Ship the widget",
        "",
        "**Needs you**",
        "Nothing needs you right now.",
        "",
        "**Being done now**",
        "- Item 1 (in progress, acme/widgets#7: open, GitHub 06:00, checked 06:01)",
        "",
        "**Comes after**",
        "- Item 2 (plan)",
    ]


def test_a_waiting_item_shows_its_record_with_every_option_and_the_pick():
    item = _item(1, status="question", pr=7, ask=RECORD)
    text = _render([item], awaits={item["item_id"]}, live=[_live(7, state="open")])
    assert (
        "- Merge PR 7? (waiting on you, acme/widgets#7: open, GitHub 06:00, checked 06:01)" in text
    )
    assert "  - Why: Only you can merge\\." in text
    assert "  - Merge: ships today" in text and "  - Hold\n" in text
    assert "  - Recommended: Merge — all checks pass" in text
    assert "Nothing needs you" not in text


def test_a_waiting_item_with_no_record_says_so_plainly():
    item = _item(1, status="question")
    text = _render([item], awaits={item["item_id"]})
    assert (
        "- Item 1 (waiting on you, checked 06:01)\n  - The reason and options were not written yet."
        in text
    )


def test_a_pick_that_is_not_one_of_the_labels_is_not_shown():
    item = _item(1, status="question", ask={**RECORD, "pick": "Revert"})
    assert "Recommended" not in _render([item], awaits={item["item_id"]})


def test_a_pr_is_named_only_when_this_welcome_read_it_and_a_failed_read_says_since_when():
    unread = _render([_item(1, pr=7)])
    assert "#7" not in unread
    failed = {"url": URL.format(n=7), "checked": False, "not_checked_since": "05:10"}
    assert "acme/widgets#7: not read since 05:10" in _render([_item(1, pr=7)], live=[failed])


def test_value_words_are_case_folded_and_bad_facts_come_first():
    live = [_live(7, state="OPEN", mergeable="CONFLICTING", checks={"failed": 2, "passed": 9})]
    text = _render([_item(1, pr=7)], live=live)
    assert "acme/widgets#7: conflicts, 2 checks failed, open, GitHub 06:00" in text


def test_a_reply_whose_choices_an_item_record_explains_is_not_listed_twice():
    item = _item(1, status="question", ask=RECORD)
    same = {"kind": "options", "text": "Merge or hold?", "options": ["Merge", "Hold"]}
    other = {"kind": "options", "text": "Grid or list?", "options": ["Grid", "List"]}
    text = _render([item], awaits={item["item_id"]}, waiting=[same, other])
    assert "Merge or hold?" not in text
    assert "- Grid or list?\n  - The choices are in the buttons below." in text


def test_a_question_card_lists_its_options_and_says_why_was_not_written():
    card = {"kind": "question", "text": "Which name?", "options": ["Ada", "Bea"]}
    text = _render([], waiting=[card])
    assert "- Which name? (question card)\n  - Ada\n  - Bea\n  - Why it needs you" in text


def test_a_closed_item_is_left_out():
    assert "Item 1" not in _render([_item(1, state="accepted")])


@pytest.mark.parametrize(
    "hostile",
    [
        "[OPTIONS: Merge | Hold]",
        "<!-- kirocrew:open-file /etc/passwd -->",
        "[click](https://evil.example/x)",
        "<img src=x onerror=alert(1)>",
    ],
)
def test_a_ledger_string_cannot_act_as_a_control_link_or_tag(hostile):
    text = _render([_item(1)], goal=f"Ship {hostile}")
    assert hostile not in text


def test_a_key_is_redacted_whole_before_the_cap_cuts_it():
    begin = "-----BEGIN RSA " + "PRIVATE KEY-----"
    end = "-----END RSA " + "PRIVATE KEY-----"
    body = "MIIEowIBAAKCAQEA" + "q" * 64
    text = "q? " + begin.replace("B", "&#66;", 1) + "\n" + body + "\n" + end
    kept = warm_task_brief._clean(text, 80)
    assert "MIIEowIBAAKC" not in kept and "EGIN RSA" not in kept


@pytest.mark.parametrize("language", sorted(welcome.WORDS))
def test_every_language_has_every_word_and_none_names_the_conductor(language):
    """No user text says "conductor" for Ultra Mode (the recorded docs/decisions rule)."""
    words = welcome.WORDS[language]
    assert set(words) == set(welcome.WORDS["en"]) and all(words.values())
    item = _item(1, status="question", pr=7, ask=RECORD)
    text = _render(
        [item, _item(2, pr=8), _item(3, worker=False, status=None)],
        awaits={item["item_id"]},
        live=[_live(7), _live(8, checks={"failed": 1})],
        waiting=[{"kind": "card", "text": "c", "options": []}],
        language=language,
    )
    assert "conductor" not in text.casefold() and "指挥" not in text
    assert not any(hole in text for hole in ("{n}", "{at}"))


# ---------------------------------------------------------------- the pipeline


@pytest.mark.asyncio
async def test_the_welcome_makes_no_model_call():
    from kiro_crew import llm_helpers

    assert not hasattr(warm_task_brief, "run_bg_oneliner")
    assert not hasattr(warm_task_brief, "_generate")
    assert llm_helpers.run_bg_oneliner is not None


# ---------------------------------------------------------------- objects fold


def _entry(seq, producer="warm_brief", target=URL.format(n=7), observed_at=10.0, **facts):
    data = {"producer": producer, "kind": "github_pull_request", "target": target,
            "fingerprint": "f", "facts": facts or {"state": "open"}, "observed_at": observed_at}  # fmt: skip
    return SimpleNamespace(type="object/observed", data=data, seq=seq)


def _fold(*entries):
    state = objects._objects_start()
    for entry in entries:
        objects._objects_step(state, entry)  # type: ignore[arg-type]
    return objects._objects_render(state)


def test_the_fold_keeps_the_newest_read_per_producer():
    out = _fold(
        _entry(1, observed_at=10.0, state="open"),
        _entry(2, producer="probe", observed_at=11.0, verdict="pass"),
        _entry(3, observed_at=12.0, state="merged"),
        # An older read folded late never overwrites a newer one.
        _entry(4, observed_at=9.0, state="draft"),
    )
    cell = out["latest"][URL.format(n=7)]
    assert cell["warm_brief"]["facts"] == {"state": "merged"}
    assert cell["probe"]["facts"] == {"verdict": "pass"}


@pytest.mark.parametrize(
    "bad",
    [
        _entry(1, producer="model"),
        _entry(1, target=""),
        _entry(1, observed_at=float("nan")),
        _entry(1, blob="x" * 3000),
    ],
)
def test_the_fold_refuses_a_record_code_did_not_make_or_cannot_bound(bad):
    assert _fold(bad)["latest"] == {}


def test_the_fold_evicts_the_least_recently_observed_subject_at_its_bound():
    out = _fold(
        *(_entry(n, target=URL.format(n=n)) for n in range(objects.OBJECTS_TARGET_LIMIT + 2))
    )
    assert len(out["latest"]) == objects.OBJECTS_TARGET_LIMIT
    assert URL.format(n=0) not in out["latest"] and out["omitted"] == 2


# ---------------------------------------------------------------- the warm_brief producer


@pytest.mark.asyncio
async def test_a_good_live_read_is_recorded_once_per_read(monkeypatch):
    seen: list[dict] = []
    from kiro_crew.crew_log import emit as crew_log_emit

    def committed(unit, **kw):
        seen.append({"unit": unit, **kw})
        kw["on_settled"](True)

    monkeypatch.setattr(crew_log_emit, "on_object_observed", committed)
    monkeypatch.setattr(inputs, "_observed", {})
    monkeypatch.setattr(inputs, "_observing", set())
    entries = [
        {"url": URL.format(n=7), "checked": True, "state": "open", "_read_epoch": 50.0},
        {"url": URL.format(n=8), "checked": False, "not_checked_since": "never"},
    ]
    await inputs._observe("unit-1", entries)
    await inputs._observe("unit-1", entries)  # the same cached read: not again
    assert [
        (s["unit"], s["producer"], s["target"], s["facts"], s["observed_at"]) for s in seen
    ] == [("unit-1", "warm_brief", URL.format(n=7), {"state": "open"}, 50.0)]


@pytest.mark.asyncio
async def test_a_read_the_writer_refused_is_recorded_again_by_the_next_read(monkeypatch):
    from kiro_crew.crew_log import emit as crew_log_emit

    answers = [False, True]
    seen: list[float] = []

    def write(unit, **kw):
        seen.append(kw["observed_at"])
        kw["on_settled"](answers.pop(0))

    monkeypatch.setattr(crew_log_emit, "on_object_observed", write)
    monkeypatch.setattr(inputs, "_observed", {})
    monkeypatch.setattr(inputs, "_observing", set())
    entries = [{"url": URL.format(n=7), "checked": True, "state": "merged", "_read_epoch": 50.0}]
    for _ in range(3):
        await inputs._observe("unit-1", entries)
    assert seen == [50.0, 50.0]
    assert inputs._observing == set()


# ---------------------------------------------------------------- the Board reader


def test_the_board_reads_pr_facts_from_the_objects_fold(monkeypatch):
    from kiro_crew import work_ledger
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import work_ledger_board as board

    url = "https://github.com/acme/w/pull/7"
    fold = {
        "latest": {
            url: {
                "warm_brief": {"kind": "k", "facts": {"state": "merged"}, "observed_at": 5.0},
                "probe": {"kind": "k", "facts": {"state": "open"}, "observed_at": 6.0},
            }
        }
    }
    monkeypatch.setattr(
        projection, "read_slot_projection", lambda *a, **k: SimpleNamespace(value=fold)
    )
    item = work_ledger.WorkItem.from_dict(
        {"item_id": "it_1", "title": "t", "state": "open",
         "acceptance": {"kind": "pr_merged", "repo": "acme/w", "pr": 7}}
    )  # fmt: skip
    facts = board.pr_facts("crew-a", [item])
    assert facts["it_1"]["label"] == "acme/w#7"
    # Only the welcome's own read is drawn.
    assert facts["it_1"]["reads"] == {
        "warm_brief": {"facts": {"state": "merged"}, "observed_at": 5.0}
    }


def test_a_cold_objects_fold_asks_for_a_prime_and_shows_no_pr_facts_yet(monkeypatch):
    from kiro_crew import work_ledger
    from kiro_crew.crew_log import projection, slot_prime
    from kiro_crew.dashboard.handlers import work_ledger_board as board

    def cold(*_args, **_kwargs):
        raise projection.SlotFoldNotReady("objects")

    primes = []
    monkeypatch.setattr(projection, "read_slot_projection", cold)
    monkeypatch.setattr(slot_prime, "schedule", lambda *a, **k: primes.append((a, k)))
    item = work_ledger.WorkItem.from_dict(
        {"item_id": "it_1", "title": "t", "state": "open",
         "acceptance": {"kind": "pr_merged", "repo": "acme/w", "pr": 7}}
    )  # fmt: skip
    assert board.pr_facts("crew-a", [item]) == {}
    # The next poll reads the cell this prime warms.
    assert primes == [(("crew-a", "objects"), {"for_reader": True})]


def test_the_snapshot_keeps_a_remote_workers_binding_for_the_liveness_readers():
    item = SimpleNamespace(
        item_id="it_1", title="t", state="open", verdict=None, decision="",
        worker_session_key="remote:w", worker_instance_id="peer-a", status="question",
        reason="needs_human", summary="", artifacts={}, last_report_at=None,
        created_at="", closed_at=None, decided_at=None, acceptance={}, pr=None,
    )  # fmt: skip
    payload = warm_task_brief._work_item_payload(item)
    assert payload["worker_instance_id"] == "peer-a"


@pytest.mark.asyncio
async def test_a_card_answered_during_the_reads_renders_again_from_the_new_read(monkeypatch):
    from dataclasses import replace

    from test_warm_task_brief import _arm_model, _claimed, _foreground_slot, _snapshot, _State

    card = {"kind": "card", "text": "Set the milestones", "options": [], "asked_at": ""}
    base = replace(_snapshot(), waiting=(card,))
    answered = replace(_claimed(base), waiting=())
    state = _State()
    slot = _foreground_slot(state)
    _arm_model(monkeypatch, slot, [base, _claimed(base), answered])
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    posted = [row["content"] for row in slot.rows if row["role"] == "assistant"]
    assert len(posted) == 1 and "Set the milestones" not in posted[0]


def test_a_cap_inside_an_address_never_leaves_it_live():
    cleaned = warm_task_brief._clean("x" * 150 + " https://evil.example/" + "a" * 80, 200)
    # The cut comes first, so the wrap around what is left is whole.
    assert cleaned.count("`") % 2 == 0
    assert cleaned.endswith("…`") or "https://" not in cleaned


def test_a_reply_that_was_only_its_choices_is_one_list_item_never_a_bare_dash():
    text = _render([], waiting=[{"kind": "options", "text": "", "options": ["Grid", "List"]}])
    assert "\n-\n" not in text and not text.endswith("\n-")
    assert "**Needs you**\n- The choices are in the buttons below." in text


def test_a_worker_reply_reads_as_an_answer_not_as_no_report_yet():
    text = _render([_item(1, status="reply")])
    assert "- Item 1 (answered a follow-up, checked 06:01)" in text
    assert "no report yet" not in text


def test_the_route_tool_and_welcome_caps_are_the_stores_own():
    from kiro_crew import mcp_work, validation, work_vocab

    fields = {f.name: f for f in validation.WORK_LEDGER_RECORD_SCHEMA.fields}
    assert fields["what"].max_len == work_vocab.WORK_ASK_WHAT_CHARS
    assert fields["why"].max_len == work_vocab.WORK_ASK_WHY_CHARS
    assert fields["pick"].max_len == work_vocab.WORK_ASK_LABEL_CHARS
    assert fields["pick_why"].max_len == work_vocab.WORK_ASK_OUTCOME_CHARS
    assert fields["options"].max_items == work_vocab.WORK_ASK_MAX_OPTIONS
    option = {f.name: f for f in fields["options"].item_schema.fields}
    assert option["label"].max_len == work_vocab.WORK_ASK_LABEL_CHARS
    assert option["outcome"].max_len == work_vocab.WORK_ASK_OUTCOME_CHARS
    (tool,) = [t for t in mcp_work._ledger_tool_definitions() if t["name"] == "work_ask_record"]
    props = tool["inputSchema"]["properties"]
    assert props["options"]["maxItems"] == work_vocab.WORK_ASK_MAX_OPTIONS
    assert props["what"]["maxLength"] == work_vocab.WORK_ASK_WHAT_CHARS
    assert props["pick_why"]["maxLength"] == work_vocab.WORK_ASK_OUTCOME_CHARS
    assert (welcome._MAX_WHY, welcome._MAX_LABEL, welcome._MAX_OUTCOME) == (
        work_vocab.WORK_ASK_WHY_CHARS,
        work_vocab.WORK_ASK_LABEL_CHARS,
        work_vocab.WORK_ASK_OUTCOME_CHARS,
    )


def test_a_cached_pr_read_shows_its_own_read_time_not_the_ledger_check():
    text = _render([_item(1, pr=7)], live=[_live(7, state="open", read_at="09:00")])
    # The ledger was checked at 06:01; GitHub was read at 09:00 and says so.
    assert "acme/widgets#7: open, GitHub 09:00, checked 06:01" in text


def test_a_running_item_keeps_its_own_name_not_its_ask():
    item = _item(1, status="question", ask=RECORD)  # re-dispatched: not waiting now
    text = _render([item])
    assert "- Item 1 (has a question" in text and "Merge PR 7?" not in text


_PAT = "ghp_" + "a1B2" * 9  # built here, so no file in the tree holds a key shape

# (ledger text, what the chat shows). Every row shows exactly the ledger text
# except where _clean means to change it: whitespace collapsed, ``**`` dropped,
# a control marker or comment opener defanged, a secret redacted.
_SHOWN_AS_WRITTEN = [
    (r"\[[click](a)\](//evil.example/x)", None),
    (r"a \ b \\ c \[ d \` e \* f", None),
    ("*a* _b_ ~~c~~ __d__ +e =f {g} ^h ~i $j %k 'l' \"m\" ,n ;o ?p !q |r| :s @t", None),
    ("# heading", None),
    ("- dash", None),
    ("1. one", None),
    ("1) one", None),
    ("> quote", None),
    ("--- rule", None),
    ("a | b | c", None),
    ("[[a](b)](//evil.example/x) !![[a](b)](//evil.example/p.png) [t][ref] [t]: /x", None),
    ("`code` and ``x`` and ` https://evil.example ` and `", None),
    ("&amp; &lt; &#x41; &copy; < > <b>x</b> <img src=x onerror=alert(1)>", None),
    ("<mcwidget>w</mcwidget> <script>x</script>", None),
    (r"\(x\) \[ y \] $x$ $$y$$ \\(z\\)", None),
    ("see https://example.com/p?q=open&author=me", None),
    ("HTTP://Example.com www.example.com <https://x.y> a.b+c@d-e.example.org", None),
    ("[t](https://evil.example) ![i](//e/p.png)", None),
    ("one\ntwo\r\n\tthree   four", "one two three four"),
    ("**bold**", "bold"),
    ("[OPTIONS: Merge | Hold]", "(OPTIONS: Merge | Hold]"),
    ("<!--- kirocrew:open-file /x -->", "<!- kirocrew:open-file /x -->"),
    (f"key {_PAT} end", "redacted"),
]


def _shown(markdown):
    """What the chat shows for *markdown*, and the code spans it holds.

    Fails when anything renders live: any block but one paragraph, or any
    inline node but text, an escape, a code span or a line break.
    """
    from markdown_it import MarkdownIt

    md = MarkdownIt("commonmark", {"html": True}).enable(["strikethrough", "table"])
    blocks = md.parse(markdown)
    assert [b.type for b in blocks] == ["paragraph_open", "inline", "paragraph_close"]
    shown, spans = [], []
    for node in blocks[1].children or []:
        assert node.type in {"text", "text_special", "code_inline", "softbreak"}, node.type
        shown.append("\n" if node.type == "softbreak" else node.content)
        if node.type == "code_inline":
            spans.append(node.content)
    return "".join(shown), spans


@pytest.mark.parametrize(("raw", "expected"), _SHOWN_AS_WRITTEN)
def test_every_ledger_string_shows_as_written_with_nothing_live(raw, expected):
    shown, spans = _shown(warm_task_brief._clean(raw, 300))
    if expected == "redacted":
        assert _PAT not in shown and shown.startswith("key ") and shown.endswith(" end")
        return
    assert shown == (raw if expected is None else expected)
    # GFM links any address it finds in text; inside a code span it finds none.
    for address in warm_task_brief._ADDRESS_RE.findall(raw):
        assert address in spans


_WIDGET_OPENERS = [
    "Document <mcwidget> syntax and the rest of the recap",
    "a <tool_use name=x> leak",
    "close </mcwidget> here",
    "see <https://example.com/p?q=<x>> and https://example.com/a<b",
]


@pytest.mark.parametrize("raw", _WIDGET_OPENERS + [raw for raw, _ in _SHOWN_AS_WRITTEN])
def test_no_cleaned_string_carries_a_literal_angle_bracket(raw):
    """The chat strips everything after a literal ``<mcwidget`` or ``<tool_use`` in
    the raw text, escaped or not, so no angle bracket survives, inside an address's
    code span either."""
    cleaned = warm_task_brief._clean(raw, 300)
    assert "<" not in cleaned and ">" not in cleaned
    if raw in _WIDGET_OPENERS:
        shown, _spans = _shown(cleaned)
        assert shown == raw


def test_an_event_only_write_changes_the_digest_the_final_re_read_compares(monkeypatch):
    """A person's steer appends an event and moves no item field, so only the
    item's newest event id tells the re-read that the rendered welcome is stale."""
    from kiro_crew import work_ledger
    from kiro_crew.dashboard.handlers import work_ledger_board as board

    item = work_ledger.WorkItem.from_dict({"item_id": "it_1", "title": "t", "state": "open"})
    closed = work_ledger.WorkItem.from_dict({"item_id": "it_2", "title": "t", "state": "accepted"})
    tail = [SimpleNamespace(id="ev_report")]
    monkeypatch.setattr(board, "_tail_events", lambda key, item_id, limit: list(tail))
    before = warm_task_brief._event_heads("crew-a", [item, closed])
    tail.append(SimpleNamespace(id="ev_steer"))
    after = warm_task_brief._event_heads("crew-a", [item, closed])
    assert before == {"it_1": "ev_report"} and after == {"it_1": "ev_steer"}
    digest = [inputs.ledger_digest(None, [item], {}, heads) for heads in (before, after)]
    assert digest[0] != digest[1]


def _question_item():
    item = _item(1, status="question")
    item["reason"] = "needs_human"
    return item


@pytest.mark.asyncio
async def test_a_worker_turn_ending_during_the_reads_renders_the_question_it_left(monkeypatch):
    """Liveness moves no ledger field: only rendering the re-read shows the change."""
    from dataclasses import replace

    from test_warm_task_brief import _arm_model, _claimed, _foreground_slot, _snapshot, _State

    from kiro_crew.dashboard.handlers import work_ledger_board as board

    base = replace(_snapshot(), work_items=(_question_item(),))
    running = iter([True])  # the worker's turn ends after the first read
    monkeypatch.setattr(board, "item_running", lambda _state, _item: next(running, False))
    state = _State()
    slot = _foreground_slot(state)
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)])
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    posted = [row["content"] for row in slot.rows if row["role"] == "assistant"]
    assert len(posted) == 1 and "**Needs you**\n- Item 1 (waiting on you" in posted[0]
    assert "Nothing needs you right now." not in posted[0]


@pytest.mark.asyncio
async def test_time_passing_alone_never_renders_again(monkeypatch):
    """Every render of one return reads one clock, so an unchanged re-read is
    the same text and one attempt posts."""
    from test_warm_task_brief import _arm_model, _claimed, _foreground_slot, _snapshot, _State

    from kiro_crew.testing.clock import ManualClock

    # Every read of the module's own clock moves it 30 s on, so "checked HH:MM"
    # would change between renders if they read it twice.
    ManualClock(start=1_800_000_000.0, tick=30.0).install(monkeypatch, warm_task_brief)
    attempts = []

    async def run_model(*_args, **_kwargs):
        attempts.append(1)

    base = _snapshot()
    state = _State()
    slot = _foreground_slot(state)
    _arm_model(monkeypatch, slot, [base, _claimed(base), _claimed(base)], run_model)
    await warm_task_brief._prepare_warm_task_brief(state, slot)
    assert len(attempts) == 1
    assert len([row for row in slot.rows if row["role"] == "assistant"]) == 1
