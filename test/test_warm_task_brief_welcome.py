"""The gateway-rendered warm welcome (RFC ``rfc-delayed-task-brief.md`` 3.10.4).

Every state, count, tag and time is the gateway's from its own reads; the conductor's
judgement is its ask record, shown as its words with their time; the model states no
fact. Also the PR facts the Board reads: the ``objects`` fold and its two producers.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from kiro_crew.crew_log import projection_objects as objects
from kiro_crew.dashboard import warm_task_brief
from kiro_crew.dashboard import warm_task_brief_inputs as inputs
from kiro_crew.dashboard import warm_task_brief_welcome as welcome
from kiro_crew.dashboard.warm_task_brief import work_item_is_terminal

URL = "https://github.com/acme/widgets/pull/{n}"
_REAL_CLOCK = inputs.clock


@pytest.fixture(autouse=True)
def _utc_clock(_floor_monkeypatch):
    _floor_monkeypatch.setattr(
        inputs, "clock", lambda e: "" if e is None else time.strftime("%H:%M", time.gmtime(e))
    )


def _item(n, *, state="open", status="question", pr=None, reported="2026-11-08T05:00:00+00:00"):
    item = {
        "item_id": f"it_{n:08x}",
        "title": f"Item {n}",
        "state": state,
        "status": status,
        "last_report_at": reported,
    }
    if pr is not None:
        item["acceptance"] = {"kind": "pr_checks", "repo": "acme/widgets", "pr": pr}
    return item


def _live(n, **facts):
    return {"url": URL.format(n=n), "pr": f"#{n}", "repo": "acme/widgets", "checked": True,
            "read_at": "06:00", **facts}  # fmt: skip


def _render(items, *, awaits=None, live=(), asks=None, anchor=None, waiting=(), waiting_total=0):
    awaits = {i["item_id"]: True for i in items} if awaits is None else awaits
    return welcome.render(
        language="en",
        items=items,
        awaits=awaits,
        waiting=waiting,
        waiting_total=waiting_total,
        live=live,
        asks=asks or {},
        anchor=anchor,
        terminal=work_item_is_terminal,
        clean=warm_task_brief._clean,
        conductor="crew-a",
    )


def test_the_count_line_names_every_must_do_and_at_most_three_cards_show():
    meta = _render([_item(n) for n in range(1, 6)])
    assert meta["count"] == 5 and meta["count_line"] == "Waiting on you: 5"
    assert [c["title"] for c in meta["cards"]] == ["Item 1", "Item 2", "Item 3"]
    assert meta["board"]["text"] == "Board: 5 waiting on you"
    # The plain text leads with the count and the first must-do.
    assert welcome.text(meta).splitlines()[0] == "Waiting on you: 5 · Item 1"


def test_an_item_not_waiting_on_the_owner_is_no_card():
    meta = _render([_item(1), _item(2, status="progress")], awaits={"it_00000001": True})
    assert [c["title"] for c in meta["cards"]] == ["Item 1"]


def test_the_conductors_record_shows_as_its_words_with_its_time():
    record = {
        "what": "Pick the layout",
        "why": "Only you know which page it is for.",
        "options": [{"label": "Two columns", "outcome": "fits wide screens"},
                    {"label": "One column", "outcome": ""}],
        "pick": "One column",
        "pick_why": "works at 320 px",
        "written_at": "2026-11-08T05:30:00+00:00",
    }  # fmt: skip
    (card,) = _render([_item(1)], asks={"it_00000001": record})["cards"]
    assert card["title"] == "Pick the layout"
    assert card["said"] == {
        "by": "Suggested 05:30",
        "why": "Only you know which page it is for.",
        "options": record["options"],
        "pick": "One column",
        "pick_why": "works at 320 px",
        "pick_label": "Recommended",
    }


@pytest.mark.parametrize(
    "record",
    [
        # No time: words nobody can date are not shown.
        {"why": "because", "options": [{"label": "A", "outcome": ""}]},
        # Nothing to say.
        {"what": "x", "written_at": "2026-11-08T05:30:00+00:00"},
    ],
)
def test_a_record_with_no_time_or_no_words_shows_nothing_of_its_own(record):
    (card,) = _render([_item(1)], asks={"it_00000001": record})["cards"]
    assert "said" not in card


def test_a_pick_outside_the_labels_is_dropped_with_its_reason():
    record = {"why": "w", "options": [{"label": "A", "outcome": ""}], "pick": "B",
              "pick_why": "because", "written_at": "2026-11-08T05:30:00+00:00"}  # fmt: skip
    (card,) = _render([_item(1)], asks={"it_00000001": record})["cards"]
    assert card["said"]["pick"] == "" and card["said"]["pick_why"] == ""


@pytest.mark.parametrize("state", ["merged", "closed"])
def test_a_merged_or_closed_pr_drops_every_ask_about_it(state):
    record = {"why": "w", "options": [{"label": "Merge it", "outcome": ""}],
              "written_at": "2026-11-08T05:30:00+00:00"}  # fmt: skip
    meta = _render([_item(1, pr=7)], live=[_live(7, state=state)], asks={"it_00000001": record})
    assert meta["cards"] == [] and meta["count"] == 0
    (change,) = meta["changes"]
    assert change["text"] == "Item 1" and change["source"] == "GitHub 06:00"
    assert "Merge it" not in welcome.text(meta)


def test_only_a_pr_this_welcome_read_is_named_and_a_failed_read_says_since_when():
    meta = _render(
        [_item(1, pr=7), _item(2, pr=8), _item(3, pr=9)],
        live=[_live(7, state="open", mergeable="conflicting"),
              {"url": URL.format(n=8), "pr": "#8", "repo": "acme/widgets", "checked": False,
               "not_checked_since": "04:10"}],
    )  # fmt: skip
    one, two, three = meta["cards"]
    assert one["pr"] == {"label": "acme/widgets#7", "url": URL.format(n=7)}
    # Bad facts first.
    assert [t["text"] for t in one["tags"]][:2] == ["conflicts", "open"]
    assert one["tags"][0]["bad"] is True and one["read"] == "GitHub 06:00"
    assert [t["text"] for t in two["tags"]][0] == "not checked since 04:10"
    # #9 was never read by this welcome: no pill, no number, anywhere.
    assert "pr" not in three and "#9" not in welcome.text(meta)


def test_changes_show_bad_facts_first_and_the_rest_go_to_the_board():
    anchor = 1_000.0
    items = [
        _item(1, status="progress", reported="2026-11-08T05:01:00+00:00"),
        _item(2, status="blocked", reported="2026-11-08T05:02:00+00:00"),
        _item(3, status="progress", reported="2026-11-08T05:03:00+00:00"),
        _item(4, status="progress", reported="2026-11-08T05:04:00+00:00"),
        _item(5, status="progress", reported="2026-11-08T05:05:00+00:00"),
    ]
    meta = _render(items, awaits={}, anchor=anchor)
    assert [c["text"] for c in meta["changes"]] == ["Item 2", "Item 5", "Item 4"]
    assert meta["changes"][0]["tags"] == [{"text": "blocked", "bad": True}]
    assert meta["more"] == "+2 more on the Board"


def test_every_language_renders_every_word():
    for language in welcome.WORDS:
        meta = welcome.render(language=language, items=[_item(1, pr=7)],
                              awaits={"it_00000001": True}, waiting=(),
                              live=[_live(7, state="open", checks={"failed": 2})], asks={},
                              anchor=None, terminal=work_item_is_terminal,
                              clean=warm_task_brief._clean, conductor="c")  # fmt: skip
        shown = json.dumps(meta, ensure_ascii=False)
        assert not any(hole in shown for hole in ("{n}", "{at}", "{w}", "{r}"))


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
    assert cell["warm_brief"]["observed_at"] == 12.0
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


def test_the_fold_ignores_other_entry_types():
    entry = SimpleNamespace(type="turn/started", data={})
    assert _fold(entry)["latest"] == {}


# ---------------------------------------------------------------- producers


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

    answers = [False, True]  # the writer's buffer is full once, then it recovers
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
    # Refused, then written by the next cached read, then never again.
    assert seen == [50.0, 50.0]
    assert inputs._observing == set()


# ---------------------------------------------------------------- model text hygiene


@pytest.mark.parametrize(
    "secret",
    [
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "AKIA**ABCDEFGHIJKLMNOP",  # rejoined once the bold markers drop
        "gh**p_" + "a" * 36,
    ],
)
def test_a_token_in_the_models_text_is_redacted_before_any_check(secret):
    cleaned = warm_task_brief._clean(f"Welcome back {secret}", 160)
    assert secret.replace("**", "") not in cleaned


def test_a_private_key_longer_than_the_cap_is_redacted_whole():
    body = "MIIEowIBAAKCAQEA" + "q" * 128
    begin, end = "-----BEGIN RSA " + "PRIVATE KEY-----", "-----END RSA " + "PRIVATE KEY-----"
    cleaned = warm_task_brief._clean(f"{begin}\n{body}\n{end}", 160)
    assert body[:12] not in cleaned


@pytest.mark.parametrize("marker", ["[OPTIONS: Merge | Hold]", "[OPTION: Merge]"])
def test_a_control_marker_in_the_models_text_cannot_go_live(marker):
    cleaned = warm_task_brief._clean(f"Welcome back {marker}", 160)
    assert marker not in cleaned


def test_a_remote_workers_turn_counts_as_running_as_on_the_board(monkeypatch):
    from kiro_crew import work_ledger
    from kiro_crew.dashboard.handlers import work_ledger_board as board

    seen: list[str] = []

    def remote(slot, item):
        seen.append(slot)
        return (True, False) if item.item_id == "it_00000001" else None

    monkeypatch.setattr(work_ledger, "remote_liveness", remote)
    assert board.item_remote_running("crew-a", _item(1), False) is True
    # A local item keeps the running state read on the loop.
    assert board.item_remote_running("crew-a", _item(2), False) is False
    assert seen == ["crew-a", "crew-a"]


def test_a_secret_in_a_gateway_string_is_redacted_on_every_path():
    secret = "AKIA" + "IOSFODNN7EXAMPLE"
    hidden = "key AKIA&#73;OSFODNN7EXAMPLE"  # one character entity-encoded
    meta = {"cards": [{"title": hidden, "tags": [{"text": "open", "bad": False}]}], "n": 3}
    scrubbed = warm_task_brief._scrubbed(meta)
    assert secret not in json.dumps(scrubbed) and "OSFODNN7EXAMPLE" not in json.dumps(scrubbed)
    assert scrubbed["cards"][0]["tags"] == meta["cards"][0]["tags"] and scrubbed["n"] == 3


def test_the_snapshot_keeps_a_remote_workers_binding_for_the_liveness_readers():
    item = SimpleNamespace(
        item_id="it_1", title="t", state="open", verdict=None, decision=None,
        worker_session_key="remote:w", worker_instance_id="inst-9", status="question",
        reason="needs_human", summary="", artifacts={}, last_report_at=None,
        created_at=None, closed_at=None, decided_at=None, acceptance={},
    )  # fmt: skip
    payload = warm_task_brief._work_item_payload(item)
    assert payload["worker_instance_id"] == "inst-9"


def test_an_ask_string_is_redacted_whole_before_it_is_cut():
    from kiro_crew.dashboard.handlers import work_ledger_board as board

    # The cap falls inside the entity-encoded key: cut first, no redactor sees it.
    text = "x" * 190 + " key AKIA&#73;OSFODNN7EXAMPLE"
    kept = board._ask_text(text, 200)
    assert "AKIA" not in kept and "OSFODNN7" not in kept


def test_the_one_pr_key_takes_the_acceptance_pr_before_the_items_own():
    item = {"pr": 9, "acceptance": {"kind": "pr_merged", "repo": "acme/w", "pr": 7}}
    assert inputs.ledger_ref(item).url == "https://github.com/acme/w/pull/7"
    assert inputs.ledger_ref({"pr": 9, "acceptance": {"repo": "acme/w"}}).url.endswith("/pull/9")
    assert inputs.ledger_ref({"pr": 9}) is None


def test_goal_board_rows_carry_the_pr_the_one_key_names():
    from kiro_crew.dashboard.handlers import work_ledger_board as board

    raw = {
        "item_id": "it_1",
        "worker_session_key": "",
        "worker_instance_id": "inst-1",  # remote: stamped too
        "pr": None,
        "acceptance": {"kind": "pr_merged", "repo": "acme/w", "pr": 7},
    }
    (out,) = board.with_worker_facts([raw], SimpleNamespace(), "")
    assert out["pr_url"] == "https://github.com/acme/w/pull/7"


def test_the_ultra_board_reads_pr_facts_by_the_one_key(monkeypatch):
    from kiro_crew import work_ledger
    from kiro_crew.crew_log import projection
    from kiro_crew.dashboard.handlers import work_ledger_board as board

    url = "https://github.com/acme/w/pull/7"
    fold = {
        "latest": {
            url: {"warm_brief": {"kind": "k", "facts": {"state": "merged"}, "observed_at": 5.0}}
        }
    }
    monkeypatch.setattr(
        projection, "read_slot_projection", lambda *a, **k: SimpleNamespace(value=fold)
    )
    item = work_ledger.WorkItem.from_dict(
        {"item_id": "it_1", "title": "t", "state": "open", "pr": 9,
         "acceptance": {"kind": "pr_merged", "repo": "acme/w", "pr": 7}}
    )  # fmt: skip
    facts = board.pr_facts("crew-a", [item])
    assert facts["it_1"]["url"] == url
    assert facts["it_1"]["reads"]["warm_brief"]["facts"] == {"state": "merged"}


def test_an_open_options_ask_counts_and_shows_with_its_labels_left_to_the_chip_bar():
    waiting = ({"kind": "options", "text": "Grid or list?", "options": ["Grid", "List"]},)
    meta = _render([], waiting=waiting)
    assert meta["count"] == 1 and meta["count_line"] == "Waiting on you: 1"
    (card,) = meta["cards"]
    assert card["kind"] == "options" and card["title"] == "Grid or list?"
    assert card["options"] == []


def test_the_count_keeps_asks_past_the_capped_waiting_list():
    waiting = tuple({"kind": "question", "text": f"Ask {n}"} for n in range(6))
    meta = _render([_item(1)], waiting=waiting, waiting_total=7)
    assert meta["count"] == 8 and meta["count_line"] == "Waiting on you: 8"
    assert len(meta["cards"]) == welcome.MAX_CARDS


def test_a_merge_never_pushes_a_bad_change_out_of_the_three_shown():
    anchor = 1_000.0
    items = [_item(n, pr=n, reported=f"2026-11-08T05:0{n}:00+00:00") for n in (5, 6, 7)] + [
        _item(1, status="blocked", reported="2026-11-08T05:01:00+00:00")
    ]
    live = [_live(n, state="merged") for n in (5, 6, 7)]
    meta = _render(items, awaits={i["item_id"]: True for i in items[:3]}, live=live, anchor=anchor)
    assert meta["changes"][0]["text"] == "Item 1"
    assert meta["changes"][0]["tags"] == [{"text": "blocked", "bad": True}]


def test_each_item_card_says_when_its_facts_were_checked():
    meta = welcome.render(
        language="en", items=[_item(1)], awaits={"it_00000001": True}, waiting=(), live=(),
        asks={}, anchor=None, terminal=work_item_is_terminal, clean=warm_task_brief._clean,
        checked_at="06:01",
    )  # fmt: skip
    # Static once posted (RFC 3.11.1): the card says when it was checked.
    assert meta["cards"][0]["checked"] == "checked 06:01"
    assert "(checked 06:01)" in welcome.text(meta)


def test_a_key_split_by_the_title_cap_is_redacted_before_the_cut():
    # The entity-encoded header sits before the cap, the body and footer after it.
    begin = "-----BEGIN RSA " + "PRIVATE KEY-----"
    end = "-----END RSA " + "PRIVATE KEY-----"
    body = "MIIEowIBAAKCAQEA" + "q" * 64
    text = "q? " + begin.replace("B", "&#66;", 1) + "\n" + body + "\n" + end
    kept = warm_task_brief._clean(text, 80)
    assert "MIIEowIBAAKC" not in kept and "EGIN RSA" not in kept


@pytest.mark.asyncio
async def test_a_return_with_no_wake_makes_no_model_call(monkeypatch):
    from kiro_crew import llm_helpers
    from kiro_crew.dashboard import warm_task_brief_wake as wake

    async def no_wake(*args, **kwargs):
        return ""

    async def must_not_run(*args, **kwargs):
        raise AssertionError("the welcome makes no one-call model call")

    monkeypatch.setattr(wake, "run", no_wake)
    monkeypatch.setattr(llm_helpers, "run_bg_oneliner", must_not_run)
    assert not hasattr(warm_task_brief, "run_bg_oneliner")
    state = SimpleNamespace(_warm_task_brief_model_active=set())
    slot = SimpleNamespace(key="s", mode="")
    snap = SimpleNamespace(original_request="", conductor=None, work_items=(), turns=(), waiting=())
    monkeypatch.setattr(warm_task_brief, "_payload", lambda *a, **k: {})
    monkeypatch.setattr(warm_task_brief, "_build_prompt", lambda *a, **k: "p")
    monkeypatch.setattr(
        warm_task_brief.KiroCrewConfig, "load", lambda: SimpleNamespace(dashboard=None)
    )
    text, _source = await warm_task_brief._generate(state, slot, snap, None)
    assert text == ""


@pytest.mark.parametrize("epoch", [-1e20, 1e20, float("nan")])
def test_a_stamp_out_of_range_reads_as_no_time(epoch):
    # The module's own clock, captured before the autouse UTC stub replaced it.
    assert _REAL_CLOCK(epoch) == ""


@pytest.mark.parametrize("language", sorted(welcome.WORDS))
def test_no_rendered_welcome_string_names_the_conductor(language):
    """No user text says "conductor" for Ultra Mode (the recorded Ultra Mode
    decision in docs/decisions), in any language the welcome speaks."""
    record = {"why": "w", "options": [{"label": "A", "outcome": ""}], "pick": "A",
              "pick_why": "p", "written_at": "2026-11-08T05:30:00+00:00"}  # fmt: skip
    items = [_item(1, pr=7), _item(2, status="blocked", reported="2026-11-08T05:40:00+00:00")]
    items[1]["decided_at"] = "2026-11-08T05:45:00+00:00"
    meta = welcome.render(
        language=language, items=items, awaits={"it_00000001": True}, waiting=(),
        live=[_live(7, state="open")], asks={"it_00000001": record}, anchor=1_000.0,
        terminal=work_item_is_terminal, clean=warm_task_brief._clean, conductor="crew-a",
        checked_at="06:01",
    )  # fmt: skip
    shown = (json.dumps(meta, ensure_ascii=False) + welcome.text(meta)).replace("crew-a", "")
    for word in welcome.WORDS[language].values():
        assert "conductor" not in word.casefold() and "指挥" not in word
    assert "conductor" not in shown.casefold().replace('"conductor":', "")
    assert "指挥" not in shown
