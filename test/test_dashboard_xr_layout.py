"""The gateway's reader of a 3D dashboard layout is held to the tab's cases.

The same file is read by ``website/src/pages/xr/workspace/dash3d.test.ts`` against the
room's own ``readLayout``: a case that one reader fails is a case the two disagree on.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.dashboard_xr_layout import WHY_LINES, Part, read_layout, room_layout, verdict

CASES = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "xr_layout_cases.json").read_text("utf-8")
)["cases"]


def _seen(p: Any) -> dict[str, Any]:
    return (
        {
            "kind": p.kind,
            "name": p.name,
            "unit": p.unit,
            "path": list(p.path),
            "max": p.max,
            "key": p.key,
            **({} if p.name_checked else {"name_checked": False}),
        }
        if isinstance(p, Part)
        else {"broken": p.broken}
    )


def test_there_are_cases() -> None:
    assert len(CASES) > 20


def _names(case: dict[str, Any]) -> list[str]:
    parts = case["layout"].get("parts") if isinstance(case["layout"], dict) else None
    if not isinstance(parts, list):
        return []
    return [p["name"] for p in parts if isinstance(p, dict) and isinstance(p.get("name"), str)]


def _py_parts_hold(case: dict[str, Any]) -> bool:
    """Whether this Python's own Unicode table (``unicodedata.unidata_version``) still lacks
    a sign of the case's names: only then is the case's ``py_parts`` this reader's answer. On
    a newer table the sign is known, the part's name is checked, and ``py_parts`` is stale."""
    return any(unicodedata.category(ch) == "Cn" for name in _names(case) for ch in name)


def _python_answer(case: dict[str, Any]) -> list[dict[str, Any]]:
    if "py_parts" in case and not _py_parts_hold(case):
        pytest.skip(f"Python's Unicode {unicodedata.unidata_version} knows every sign of this case")
    return case.get("py_parts", case["parts"])


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_layout_reads_as_the_room_reads_it(case: dict[str, Any]) -> None:
    layout = read_layout(case["layout"])
    assert layout.why == case["why"]
    # Where the two Unicode tables differ, the Python reader's own answer (`py_parts`).
    assert [_seen(p) for p in layout.parts] == _python_answer(case)


TABLES_DIFFER = [c for c in CASES if "py_parts" in c]


@pytest.mark.parametrize("case", TABLES_DIFFER, ids=[c["name"] for c in TABLES_DIFFER])
def test_the_two_readers_agree_where_their_tables_differ(case: dict[str, Any]) -> None:
    """M83: the gateway's answer held to the tab's (`parts`, which `dash3d.test.ts` holds the
    tab to), whatever this Python's table: the same count; each place the same, or Python says
    its name was not checked and the tab has the same part or a bad name."""
    py = [_seen(p) for p in read_layout(case["layout"]).parts]
    tab = case["parts"]
    assert len(py) == len(tab)
    for mine, theirs in zip(py, tab):
        if mine == theirs:
            continue
        assert mine.get("name_checked") is False
        same = {k: v for k, v in mine.items() if k != "name_checked"}
        assert same == theirs or theirs == {"broken": "name"}


def test_a_key_absent_reads_state_and_a_path_splits_on_dots() -> None:
    layout = read_layout(
        {"v": 1, "parts": [{"kind": "lamps", "name": "F", "field": "f", "path": "a.b"}]}
    )
    (part,) = layout.parts
    assert isinstance(part, Part)
    assert part.key == "state" and part.path == ("a", "b")


def test_a_number_limit_that_is_not_finite_or_true_breaks_the_part() -> None:
    for bad in (float("inf"), float("nan"), True, -1):
        (part,) = read_layout(
            {"v": 1, "parts": [{"kind": "bar", "name": "B", "field": "b", "max": bad}]}
        ).parts
        assert not isinstance(part, Part), bad


# The verdict line the write's reply carries (D103 5).

EN = json.loads(
    (
        Path(__file__).resolve().parent.parent / "website" / "src" / "i18n" / "locales" / "en.json"
    ).read_text("utf-8")
)


def _room_lines(layout: Any) -> list[str]:
    """The lines the room writes on the taken form's lip for what is wrong, in order."""
    if layout.why:
        return [WHY_LINES[layout.why]]
    return [WHY_LINES[p.broken] for p in layout.parts if not isinstance(p, Part)]


def test_the_lines_are_the_rooms_own_words() -> None:
    """Each line is the tab's English catalogue word for word (`xr.dash3d_why_*`)."""
    xr = EN["xr"]
    assert WHY_LINES == {k: xr[f"dash3d_why_{k}"] for k in WHY_LINES}
    assert set(WHY_LINES) == {k[len("dash3d_why_") :] for k in xr if k.startswith("dash3d_why_")}


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_the_reply_says_what_the_room_writes(case: dict[str, Any]) -> None:
    """The same words in the reply as on the lip, in the same order; each part by its
    place and kind; none of the Bot's own text."""
    raw = case["layout"]
    line = verdict(raw)
    if raw is None or raw == {}:
        # The room draws no dashboard for it (`layoutOf`).
        assert line == "In the XR room: no 3D dashboard."
        return
    # Built from the CASE (what the room reads) and the tab's catalogue, not from `verdict`.
    room = EN["xr"]
    if case["why"]:
        want = f"In the XR room this layout is an empty grey tray: {room['dash3d_why_' + case['why']]}."
    else:
        each = [
            f"part {i}: "
            + (
                "name not checked"
                if p.get("name_checked") is False
                else p["kind"] if "kind" in p else room["dash3d_why_" + p["broken"]]
            )
            for i, p in enumerate(_python_answer(case), start=1)
        ]
        want = "In the XR room this layout reads: " + "; ".join(each) + "."
    assert line == want
    for p in case["parts"]:
        if len(p.get("name", "")) >= 3:
            assert p["name"] not in line


def test_a_cleared_layout_and_null_say_no_3d_dashboard() -> None:
    assert verdict(None) == "In the XR room: no 3D dashboard."
    assert verdict({}) == "In the XR room: no 3D dashboard."
    assert room_layout({}) is None


def test_the_reply_is_made_of_a_closed_list_of_words() -> None:
    """Every word of every reply comes from the fixed pieces: no name, field or number of
    the layout reaches it (REVIEW-41 5.3 1)."""
    import re

    from kiro_crew.dashboard_xr_layout import (
        GREY_TRAY,
        KIND_WORDS,
        NAME_NOT_CHECKED,
        NO_DASHBOARD,
        READS,
    )

    pieces = [
        NO_DASHBOARD,
        GREY_TRAY,
        READS,
        NAME_NOT_CHECKED,
        *WHY_LINES.values(),
        *KIND_WORDS.values(),
    ]
    allowed = {w for piece in pieces for w in re.findall(r"[A-Za-z0-9]+", piece)}
    allowed |= {"part", *(str(i) for i in range(1, 7))}
    for case in CASES:
        words = re.findall(r"[A-Za-z0-9]+", verdict(case["layout"]))
        assert set(words) <= allowed, (case["name"], set(words) - allowed)


def test_ten_thousand_parts_answer_in_bounded_work_and_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded by work, not by a clock: not one part of a list past six is read."""
    import kiro_crew.dashboard_xr_layout as xl

    reads: list[Any] = []
    real = xl.read_part
    monkeypatch.setattr(xl, "read_part", lambda p: reads.append(p) or real(p))
    raw = {"v": 1, "parts": [{"kind": "ring", "name": "N", "field": "f", "max": 1}] * 10_000}
    line = verdict(raw)
    assert line == f"In the XR room this layout is an empty grey tray: {WHY_LINES['too_many']}."
    assert reads == []


def test_six_huge_names_answer_in_bounded_length_with_none_of_their_text() -> None:
    big = "Z" * 200_000
    raw = {"v": 1, "parts": [{"kind": "number", "name": big, "field": "f"}] * 6}
    line = verdict(raw)
    assert line is not None and len(line) < 300
    assert "ZZZ" not in line
    assert line.count(WHY_LINES["name"]) == 6


def test_a_name_with_a_sign_this_table_does_not_know_is_never_said_good() -> None:
    """M76: the reply says "name not checked", never the part's kind, for such a name."""
    line = verdict(
        {"v": 1, "parts": [{"kind": "ring", "name": "A\U0001fae9", "field": "f", "max": 2}]}
    )
    assert line == "In the XR room this layout reads: part 1: name not checked."


# ── what each part shows (D169) ─────────────────────────────────────────────

VALUE_CASES = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "xr_layout_cases.json").read_text("utf-8")
)["value_cases"]


def _good_parts(layout: Any) -> list[Part]:
    return [p for p in read_layout(layout).parts if isinstance(p, Part)]


def test_there_are_value_cases_and_each_names_every_part() -> None:
    assert len(VALUE_CASES) > 10
    for case in VALUE_CASES:
        assert len(_good_parts(case["layout"])) == len(case["says"]), case["name"]


@pytest.mark.parametrize("case", VALUE_CASES, ids=[c["name"] for c in VALUE_CASES])
def test_a_part_says_what_the_room_shows(case: dict[str, Any]) -> None:
    """The same file holds the tab's ``partSays`` (``dashSays.test.ts``): one answer."""
    from kiro_crew.dashboard_xr_layout import part_says

    assert [part_says(p, case["fields"]) for p in _good_parts(case["layout"])] == case["says"]


@pytest.mark.parametrize("case", VALUE_CASES, ids=[c["name"] for c in VALUE_CASES])
def test_the_layouts_line_carries_each_parts_words(case: dict[str, Any]) -> None:
    from kiro_crew.dashboard_xr_layout import READS

    want = "; ".join(f"part {i}: {says}" for i, says in enumerate(case["says"], start=1))
    assert verdict(case["layout"], case["fields"]) == f"{READS} {want}."
    # With no values (the page's read failed) the line names each part's kind only.
    kinds = "; ".join(
        f"part {i}: {p.kind}" for i, p in enumerate(_good_parts(case["layout"]), start=1)
    )
    assert verdict(case["layout"]) == f"{READS} {kinds}."


def test_a_number_is_written_whole_or_with_two_places_a_half_away_from_zero() -> None:
    from kiro_crew.dashboard_xr_layout import SAYS, say_number

    given = [0.0, -0.0, 7, 100, 1200, 12.5, 0.125, -0.125, 1.005, 2.999, -3.456, 0.001]
    want = ["0", "0", "7", "100", "1200", "12.5", "0.13", "-0.13", "1", "3", "-3.46", "0"]
    assert [say_number(float(n)) for n in given] == want
    assert say_number(999999999999.0) == "999999999999"
    assert [say_number(n) for n in (1e12, -1e12, 1e300)] == [SAYS["big"]] * 3


FOUR = next(c["layout"] for c in VALUE_CASES if c["name"] == "every part has its value")
FOUR_FIELDS = next(c["fields"] for c in VALUE_CASES if c["name"] == "every part has its value")


def test_a_written_field_is_answered_for_the_parts_that_read_it_only() -> None:
    """D169 3: as a part's own field or as its limit's; a field two parts read names
    both; a field no part reads, no layout, or a document that is no layout: nothing."""
    from kiro_crew.dashboard_xr_layout import SHOWS, field_verdict

    page = {**FOUR_FIELDS, "xr_layout": FOUR}
    assert field_verdict("checks", page) == f"{SHOWS} part 3: ring 12/20."
    assert field_verdict("budget", page) == f"{SHOWS} part 2: bar 12.5 $ of 40."
    assert field_verdict("unread", page) == ""
    assert field_verdict("checks", FOUR_FIELDS) == ""
    assert field_verdict("checks", {**FOUR_FIELDS, "xr_layout": {}}) == ""
    assert field_verdict("checks", {**FOUR_FIELDS, "xr_layout": {"v": 1, "parts": []}}) == ""
    assert field_verdict("checks", None) == ""
    two = {
        "v": 1,
        "parts": [
            {"kind": "number", "name": "A", "field": "n"},
            {"kind": "gauge", "name": "B", "field": "n"},
            {"kind": "ring", "name": "C", "field": "n", "max": "n"},
        ],
    }
    # A wrong part is not named for a number's write: its reason came with the layout's.
    assert field_verdict("n", {"xr_layout": two, "n": 4}) == (
        f"{SHOWS} part 1: number 4; part 3: ring 4/4."
    )


def test_the_values_words_are_a_closed_list_and_no_text_of_the_bot_comes_back() -> None:
    """Fixed words, a place number, a kind, a unit and numbers: a part's name, a field's
    name and any text a field holds never reach the line (both lines)."""
    import re

    from kiro_crew.dashboard_xr_layout import (
        DASH_KINDS,
        DASH_UNITS,
        LAMP_STATES,
        NAME_NOT_CHECKED,
        READS,
        SAYS,
        SHOWS,
        field_verdict,
    )

    pieces = [READS, SHOWS, NAME_NOT_CHECKED, *SAYS.values(), *WHY_LINES.values()]
    allowed = {w for piece in pieces for w in re.findall(r"[A-Za-z]+", piece)}
    allowed |= {"part", "of", *DASH_KINDS, *LAMP_STATES}
    allowed |= {w for u in DASH_UNITS for w in re.findall(r"[A-Za-z]+", u)}
    secret = "Zebra quartz 4242 hunter2"
    hostile = {
        "v": 1,
        "parts": [
            {"kind": "number", "name": "Zebra", "field": "zebra", "unit": "quartz"},
            {"kind": "bar", "name": "Quartz", "field": "zebra", "max": "zebra_max", "unit": "ms"},
            {"kind": "lamps", "name": "Rows", "field": "rows", "key": "hunter2"},
            {"kind": "number", "name": "Deep", "field": "deep", "path": "zebra.quartz"},
        ],
    }
    fields = {
        "zebra": secret,
        "zebra_max": secret,
        "rows": [{"hunter2": secret}, secret, {"hunter2": "done"}],
        "deep": {"zebra": {"quartz": secret}},
    }
    lines = [verdict(hostile, fields)]
    lines += [field_verdict(name, {**fields, "xr_layout": hostile}) for name in fields]
    lines += [verdict(c["layout"], c["fields"]) for c in VALUE_CASES]
    for line in lines:
        words = re.findall(r"[A-Za-z]+", line)
        assert set(words) <= allowed, set(words) - allowed
        for word in ("Zebra", "zebra", "quartz", "hunter2", "4242"):
            assert word not in line


def test_the_line_is_bounded_whatever_the_layout_and_the_values_hold() -> None:
    """20 parts are one line of the grey tray; six parts of the longest words stay short;
    huge values cost no more words."""
    from kiro_crew.dashboard_xr_layout import GREY_TRAY, field_verdict

    part = {"kind": "bar", "name": "N", "field": "n", "max": "m", "unit": "/s"}
    twenty = {"v": 1, "parts": [part] * 20}
    assert verdict(twenty, {"n": 1, "m": 2}) == f"{GREY_TRAY} {WHY_LINES['too_many']}."
    six = {"v": 1, "parts": [part] * 6}
    worst = {"n": -123456789012.345, "m": 0.01, "rows": ["x" * 100_000] * 100_000}
    assert len(verdict(six, worst)) < 600
    assert len(field_verdict("n", {**worst, "xr_layout": six})) < 600
    lamps = {"v": 1, "parts": [{"kind": "lamps", "name": "L", "field": "rows"}] * 6}
    assert len(verdict(lamps, worst)) < 600


def test_a_name_that_is_not_checked_still_says_what_its_part_shows() -> None:
    from kiro_crew.dashboard_xr_layout import NAME_NOT_CHECKED, READS

    unknown = next(
        chr(c) for c in range(0x1FAFF, 0x1F000, -1) if unicodedata.category(chr(c)) == "Cn"
    )
    layout = {"v": 1, "parts": [{"kind": "number", "name": f"A{unknown}", "field": "n"}]}
    assert verdict(layout, {"n": 3}) == f"{READS} part 1: {NAME_NOT_CHECKED}, number 3."


def test_values_of_any_shape_never_raise() -> None:
    from kiro_crew.dashboard_xr_layout import field_verdict, part_says

    part = _good_parts(FOUR)[1]
    for fields in (None, [], "x", 7, {"spend": {"a": 1}}, {"spend": 10**400, "budget": 1}):
        assert part_says(part, fields).startswith("bar, a dash: ")  # type: ignore[arg-type]
        assert isinstance(field_verdict("spend", fields), str)  # type: ignore[arg-type]


# ── one rule for a plain number, in both readers (M175, M176, M182) ─────────

_FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "xr_layout_cases.json").read_text("utf-8")
)
PLAIN_CASES = _FIXTURE["plain_number_cases"]


@pytest.mark.parametrize("text,number", PLAIN_CASES, ids=[repr(c[0]) for c in PLAIN_CASES])
def test_a_text_is_the_plain_number_the_room_reads(text: str, number: float | None) -> None:
    """The same file holds the tab's ``plainNumber`` (``plainNumber.test.ts``)."""
    from kiro_crew.dashboard_xr_layout import _plain_number

    assert _plain_number(text) == number


def test_the_spaces_round_a_number_are_one_written_out_list() -> None:
    """Not ``\\s``: the tab's and Python's differ. The cases file holds the list, and the
    tab's reader is held to the same one."""
    from kiro_crew.dashboard_xr_layout import PLAIN_SPACES

    assert list(PLAIN_SPACES) == _FIXTURE["plain_number_spaces"]
    assert len(set(PLAIN_SPACES)) == len(PLAIN_SPACES)


def _the_rule_before(text: str) -> float | None:
    """The rule as it stood at ``7b0b8b6e38e``: Python's own ``\\s``, in one pattern."""
    import math
    import re

    if not re.fullmatch(r"\s*-?[0-9]+(?:[.,][0-9]+)*\s*%?\s*", text):
        return None
    bare = re.sub(r"([0-9]),([0-9]{3})", r"\1\2", re.sub(r"[%\s]", "", text)).replace(",", ".", 1)
    try:
        n = float(bare)
    except ValueError:
        return None
    return n if math.isfinite(n) else None


def test_the_new_rule_reads_as_the_old_one_but_for_the_six_signs_of_m176() -> None:
    """Every short text made of a number's signs and one other sign, at each place: the
    two rules differ only where the text holds a sign that the tab's ``\\s`` and Python's
    disagree on. There the new rule follows the tab."""
    from kiro_crew.dashboard_xr_layout import _plain_number

    odd = {0xFEFF, 0x1C, 0x1D, 0x1E, 0x1F, 0x85}
    signs = [chr(c) for c in (*range(0x00, 0x100), 0x1680, *range(0x1FFE, 0x2012), 0x2028, 0x2029)]
    signs += [chr(c) for c in (0x202F, 0x205F, 0x2060, 0x3000, 0x3001, 0xFEFF, 0xFF11, 0x0661)]
    shapes = [
        "{c}12",
        "12{c}",
        "1{c}2",
        "12{c}%",
        "12%{c}",
        "{c}-1,234.5 %",
        "{c}",
        "{c}{c}",
        "1,234{c},567",
    ]
    differ: set[int] = set()
    for c in signs:
        for shape in shapes:
            text = shape.format(c=c)
            if _plain_number(text) != _the_rule_before(text):
                differ.add(ord(c))
    assert differ == odd


def _comma_family() -> list[str]:
    """9,360 texts: one to four groups of one to four digits, joined by commas and points."""
    import itertools

    out: list[str] = []
    for count in range(1, 5):
        for groups in itertools.product(("7", "45", "123", "0123"), repeat=count):
            for joins in itertools.product(",.", repeat=count - 1):
                core = groups[0] + "".join(j + g for j, g in zip(joins, groups[1:]))
                out += [sign + core + tail for sign in ("", "-") for tail in ("", " %")]
    return out


def _has_thousands(text: str) -> bool:
    """Written apart from the reader's pattern: every comma before exactly three digits."""
    core = text.removesuffix(" %").removeprefix("-")
    whole, point, rest = core.partition(".")
    first, *groups = whole.split(",")
    return (
        bool(groups)
        and 1 <= len(first) <= 3
        and all(len(g) == 3 for g in groups)
        and "," not in rest
        and "." not in rest
        and (not point or rest.isdigit())
    )


def test_all_the_commas_of_a_number_with_thousands_go_out_and_nothing_else_changes() -> None:
    """ "1,234,567" read 1234.567: the replace took "1,234" and with it the digit the next
    comma needs before it, so that comma stayed and became the point. Now a text whose
    commas all stand before groups of three reads without them. Over the family, the
    only texts that read otherwise than by the rule before are of that kind and hold
    two commas or more; one comma ("1,234") read 1234 before and does now."""
    from kiro_crew.dashboard_xr_layout import _plain_number

    family = _comma_family()
    assert len(family) == 9360
    changed = [t for t in family if _plain_number(t) != _the_rule_before(t)]
    assert changed
    for text in changed:
        assert _has_thousands(text) and text.count(",") >= 2, text
    for text in family:
        if _has_thousands(text):
            bare = text.removesuffix(" %").replace(",", "")
            assert _plain_number(text) == float(bare), text
    assert len(changed) == len([t for t in family if _has_thousands(t) and t.count(",") >= 2])
    assert _plain_number("1,234") == _the_rule_before("1,234") == 1234


def _six_parts_read(text: str) -> str:
    six = {"v": 1, "parts": [{"kind": "number", "name": "N", "field": "n"}] * 6}
    return verdict(six, {"n": text})


@pytest.mark.parametrize(
    "pump",
    [
        lambda n: "1" + " " * n + "x",
        lambda n: " " * n,
        lambda n: "1 1 " * n,
        lambda n: "1" * n + "x",
        lambda n: "1" + "\t " * n + "%" + " " * n + "x",
        lambda n: "1" + ",1" * n + "x",
    ],
    ids=[
        "one-spaces-x",
        "spaces",
        "one-space-pairs",
        "digits-x",
        "spaces-percent-spaces-x",
        "comma-groups-x",
    ],
)
def test_a_long_text_that_is_almost_a_number_is_read_in_linear_time(pump: Any) -> None:
    """M175: six parts on one field of 8 kB took 1.2 s with the old pattern, which tried
    each way to split a run of spaces round the percent sign. Thread CPU, a ramp, then
    pumps of 200, 2,000 and 20,000 units held to a ratio (``conftest``)."""
    from conftest import assert_rejected_without_backtracking

    def reject(text: str) -> None:
        line = _six_parts_read(text)
        assert line.count("a dash: not a number") == 6, line[:120]

    assert_rejected_without_backtracking(reject, pump)


def test_a_long_text_that_is_a_number_or_too_long_for_one_is_read_too() -> None:
    assert "number 12;" in _six_parts_read(" " * 4000 + "12" + " " * 4000 + "%" + " " * 180)
    # 8,192 digits are no finite number: a dash, as the room draws.
    assert _six_parts_read("1" * 8192).count("a dash: not a number") == 6
