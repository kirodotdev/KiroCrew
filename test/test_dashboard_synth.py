"""Tests for ``dashboard_synth`` -- the synthetic copy of a dashboard read.

The copy is what a browser is handed in place of the crewmate's record, so what these
pin is the two halves of that promise: the copy has the record's SHAPE, so a page takes
the same paths through its code, and it holds none of the record's CONTENT.
"""

from __future__ import annotations

import copy
import datetime
import inspect
import json
import random
import re
import unicodedata
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew import dashboard_frame, dashboard_synth

DAY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
ISO = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:[Zz]|[+-]\d{2}(?::?\d{2})?)?)?\Z"
)
ITEM_ID = re.compile(r"it_[0-9a-f]{8}\Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")

#: A record shaped like the folds' own output: rows with ids that refer to each other,
#: enum words, stamps in several spellings, text in two scripts, a map keyed by a runtime
#: id, nulls and missing fields. Every free value is long and distinctive enough that its
#: appearance anywhere in a copy could not be chance.
FIELDS: dict[str, Any] = {
    "open_prs": 48213,
    "ratio": 0.3756,
    "cost": -1234.5,
    "whole_float": 7213.0,
    "headline": "Quarterly \u8ba2\u5355 reconciliation of the ZXQ-VAULT ledger\nsecond line  end",
    "state": "accepted",
    "tone": "green",
    "done": True,
    "blocked": False,
    "nothing": None,
    "zero": 0,
    "empty": "",
    "stamp": "2026-10-15T09:25:14.123Z",
    "stamp_offset": "2026-10-14 01:02:03+08:00",
    "day": "2026-10-13",
    "who": "it_e3c6afff",
    "run": "1bec53d0-e2ea-4190-8ac8-689ed9cb168d",
    "chat": "chat-2825",
    "sha": "9f2c1d7a45b0e8c3a6d7f1e2b4c5a8d9e0f1a2b3",
    "link": "https://github.com/kirodotdev/KiroCrew/pull/20747",
    "digits": "20640718",
    "items": [
        {
            "item_id": "it_e3c6afff",
            "title": "Wire the \u6821\u9a8c check into dashboard_save",
            "state": "accepted",
            "verdict": None,
            "fails": 2417,
            "pr": 20747,
            "created_at": "2026-10-14T01:02:03Z",
            "parent_item": "it_e3c6afff",
            "tags": ["urgent-ZXQ", "\u6821\u9a8c"],
        },
        {
            "item_id": "it_00aa11bb",
            "title": "",
            "state": "open",
            "verdict": "pass",
            "fails": 0,
            "pr": None,
            "created_at": "2026-10-15T01:02:03Z",
            "parent_item": "it_e3c6afff",
            "tags": [],
        },
    ],
    "by_model": {"claude-opus-4-xyz": {"turns": 4821}, "gpt-9-secret": {"turns": 377}},
}

MODEL: dict[str, Any] = {
    "types": {
        "tone": {"type": "enum", "choices": ["green", "amber", "red"], "source": {"agentic": True}},
        "by_model": {"type": "object", "source": {"fold": "usage", "path": "by_model"}},
        "items": {"type": "array", "source": {"fold": "work", "path": "items"}},
    }
}


#: A page that spells out the keys of the fixture's rows, as a page that draws them does.
PAGE = """
<script>
  window.kirocrew.fields.items.map(function (row) {
    return [row.item_id, row.title, row.state, row.verdict, row.fails, row.pr,
            row.created_at, row.parent_item, row.tags];
  });
  Object.values(window.kirocrew.fields.by_model).map(function (m) { return m.turns; });
</script>
"""


def _read(**overrides: Any) -> dict[str, Any]:
    fields = {**FIELDS, **overrides}
    return dashboard_frame.read_payload(
        fields,
        agentic=["headline", "tone"],
        seq=914827,
        missing=["nothing"],
        written_at={"headline": "2026-10-15T09:00:00Z", "tone": "2026-10-15T09:00:01Z"},
        last_user_at="2026-10-15T08:59:59Z",
    )


#: The instant a copy's timestamps are drawn back from, so no test reads the clock.
NOW = datetime.datetime(2026, 10, 15, 12, 0, 0, tzinfo=datetime.timezone.utc)


def _copy(
    read: dict[str, Any] | None = None,
    seed: int = 0,
    model: dict[str, Any] | None = MODEL,
    now: datetime.datetime = NOW,
    page: str = PAGE,
) -> dict[str, Any]:
    return dashboard_synth.synthesize(
        read or _read(), model=model, page=page, rng=random.Random(seed), now=now
    )


def _scalars(value: Any) -> Iterator[Any]:
    """Every string and number VALUE in *value*: map keys are names and are not content."""
    if isinstance(value, dict):
        for inner in value.values():
            yield from _scalars(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _scalars(inner)
    elif isinstance(value, bool) or value is None:
        return
    else:
        yield value


def _content(read: dict[str, Any]) -> Iterator[Any]:
    """The values of a read that are the record's: not the names, flags and tags."""
    for key, value in read.items():
        if key not in ("agentic", "missing", "locale", "subject"):
            yield from _scalars(value)


def _kinds(text: str) -> str:
    """One symbol per character: its kind when it is a letter, digit or ideograph, the
    character itself when it is a space, a line break or punctuation."""
    out = []
    for char in text:
        if "a" <= char <= "z":
            out.append("l")
        elif "A" <= char <= "Z":
            out.append("u")
        elif "0" <= char <= "9":
            out.append("d")
        elif "\u4e00" <= char <= "\u9fa5":
            out.append("h")
        else:
            out.append(char)
    return "".join(out)


def _same_shape(real: Any, fake: Any, path: str = "read") -> None:
    """Assert that *fake* has the shape of *real*: keys, lengths, types, nulls, booleans."""
    if isinstance(real, dict):
        assert isinstance(fake, dict), path
        # An opaque map's keys are invented, so its keys are compared by count.
        assert len(real) == len(fake), path
        if set(real) == set(fake):
            for key in real:
                _same_shape(real[key], fake[key], f"{path}.{key}")
        else:
            for (_k1, v1), (_k2, v2) in zip(real.items(), fake.items()):
                _same_shape(v1, v2, f"{path}.<key>")
    elif isinstance(real, list):
        assert isinstance(fake, list) and len(real) == len(fake), path
        for index, (a, b) in enumerate(zip(real, fake)):
            _same_shape(a, b, f"{path}[{index}]")
    elif real is None or isinstance(real, bool):
        assert fake is real, path
    elif isinstance(real, int):
        assert isinstance(fake, int) and not isinstance(fake, bool), path
        assert (real < 0) == (fake < 0) and (real == 0) == (fake == 0), path
        assert len(str(abs(real))) == len(str(abs(fake))), path
    elif isinstance(real, float):
        assert isinstance(fake, float), path
        assert (real < 0) == (fake < 0), path
        assert real.is_integer() == fake.is_integer(), path
        assert len(repr(abs(real))) == len(repr(abs(fake))), path
    else:
        assert isinstance(real, str) and isinstance(fake, str), path
        if path == "read.fields.tone":
            # An enum field shows one of its declared labels, whatever the record held.
            assert fake in ("green", "amber", "red"), path
        else:
            assert len(real) == len(fake), path


# --------------------------------------------------------------------------- #
# 1. The shape
# --------------------------------------------------------------------------- #


def test_the_copy_has_exactly_the_shape_of_the_record():
    real = _read()

    fake = _copy(real)

    assert list(fake) == list(real), "the read's own keys and their order"
    _same_shape(real, fake)


def test_free_text_keeps_its_length_and_the_kind_of_every_character():
    text = FIELDS["headline"]

    fake = _copy()["fields"]["headline"]

    assert fake != text
    assert _kinds(fake) == _kinds(text)
    # The line break and the double space are where they were.
    assert [i for i, c in enumerate(fake) if c in "\n "] == [
        i for i, c in enumerate(text) if c in "\n "
    ]


def test_a_number_keeps_its_sign_its_size_and_whether_it_is_whole():
    fields = _copy()["fields"]

    assert isinstance(fields["open_prs"], int) and len(str(fields["open_prs"])) == 5
    assert fields["cost"] < 0 and len(repr(fields["cost"])) == len(repr(-1234.5))
    assert not fields["ratio"].is_integer() and 0.1 <= fields["ratio"] < 1
    assert isinstance(fields["whole_float"], float) and fields["whole_float"].is_integer()
    assert 1000 <= fields["whole_float"] < 10000
    assert fields["items"][0]["fails"] >= 1000


def test_zero_an_empty_text_and_null_stay_as_they_are():
    fields = _copy()["fields"]

    assert fields["zero"] == 0 and isinstance(fields["zero"], int)
    assert fields["empty"] == ""
    assert fields["nothing"] is None
    assert fields["items"][0]["verdict"] is None and fields["items"][1]["pr"] is None
    assert fields["items"][1]["title"] == "" and fields["items"][1]["tags"] == []


def test_booleans_are_kept():
    fields = _copy()["fields"]

    assert fields["done"] is True and fields["blocked"] is False


def test_dates_and_ids_stay_in_their_formats():
    fields = _copy()["fields"]

    assert ISO.fullmatch(fields["stamp"]) and fields["stamp"].endswith("Z")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", fields["stamp"])
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}", fields["stamp_offset"]
    )
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", fields["day"])
    assert ITEM_ID.fullmatch(fields["who"])
    assert UUID.fullmatch(fields["run"])
    # Not an item id, so the word in front of it is the record's text and is replaced.
    assert re.fullmatch(r"[a-z]{4}-\d{4}", fields["chat"]) and not fields["chat"].startswith("chat")
    assert re.fullmatch(r"[0-9a-f]{40}", fields["sha"])
    assert fields["digits"].isdigit() and len(fields["digits"]) == 8 and fields["digits"][0] != "0"
    whole = _copy()
    for key in ("headline", "tone"):
        assert ISO.fullmatch(whole["written_at"][key])
    assert ISO.fullmatch(whole["last_user_at"])


def test_a_link_becomes_an_example_invalid_link_of_the_same_length():
    link = _copy()["fields"]["link"]

    assert link.startswith("https://example.invalid/")
    assert len(link) == len(FIELDS["link"])


def test_a_timestamp_is_a_real_date_no_later_than_now():
    for seed in range(30):
        stamp = _copy(seed=seed)["fields"]["stamp"]
        moment = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        assert NOW - datetime.timedelta(days=366) <= moment <= NOW


def test_the_clock_is_the_one_the_caller_gives_and_nothing_else_moves_a_seeded_copy():
    later = NOW + datetime.timedelta(days=400)

    assert _copy(seed=7) == _copy(seed=7), "a given generator and clock are reproducible"
    assert _copy(seed=7)["fields"]["stamp"] != _copy(seed=7, now=later)["fields"]["stamp"]


def test_only_a_work_item_id_keeps_the_word_in_front_of_it():
    """A word before a separator is the record's text unless the schema fixes it."""
    from kiro_crew import work_ledger

    free_text = [
        "ProjectAtlas-2026",
        "Atlas_20260101",
        "secret:123456",
        "release-2026-10",
        "ZXQ-1234abcd",
        "payroll_9f2c1d7a45",
        "It_E3C6AFFF",
        "it_zzzzzzzz",
        "it_e3c6aff",
    ]
    real = _read(**{f"v{n}": text for n, text in enumerate(free_text)})
    for seed in range(20):
        fake = _copy(real, seed=seed)["fields"]
        for n, text in enumerate(free_text):
            word = re.split(r"[-_:]", text)[0]
            assert word.lower() not in fake[f"v{n}"].lower(), (text, fake[f"v{n}"])
            assert len(fake[f"v{n}"]) == len(text)
    for _ in range(20):
        minted = work_ledger.mint_item_id()
        assert dashboard_synth._ITEM_ID.fullmatch(minted)
        replaced = _copy(_read(who=minted), seed=3)["fields"]["who"]
        assert work_ledger._ITEM_ID_RE.match(replaced), replaced


def test_a_symbol_or_an_emoji_stays_a_symbol_of_its_own_width():
    text = "Launch \u2605 now \U0001f680 done \u2713"

    rocket_seen: set[str] = set()
    for seed in range(60):
        fake = _copy(_read(note=text), seed=seed)["fields"]["note"]
        assert len(fake) == len(text)
        for before, after in zip(text, fake):
            if unicodedata.category(before) == "So":
                assert unicodedata.category(after) == "So"
                assert (ord(before) > 0xFFFF) == (ord(after) > 0xFFFF)
        rocket_seen.add(fake[text.index("\U0001f680")])
        assert "\U0001f680" not in fake, "the pool has no rocket"
    assert len(rocket_seen) > 1


def test_a_combining_mark_is_replaced_by_a_combining_mark():
    text = "e\u0301 a\u0308 o\u0323"

    fake = _copy(_read(note=text), seed=5)["fields"]["note"]

    assert len(fake) == len(text)
    assert [unicodedata.category(c) for c in fake] == [unicodedata.category(c) for c in text]
    assert "\u0323" not in fake


# --------------------------------------------------------------------------- #
# 2. The content
# --------------------------------------------------------------------------- #


def test_no_value_of_the_record_is_in_the_copy():
    real = _read()
    kept = dashboard_synth.fixed_vocabulary() | set(MODEL["types"]["tone"]["choices"])
    # Zero is the one number with nothing in it but "none", and it is kept; the words
    # of the closed vocabularies are kept on purpose. A draw is independent of the value it
    # replaces, so it equals that value at the rate it equals any other: a calendar day is
    # one of a few hundred and can come out the same by chance, which is not what this
    # pins. Every other value has room enough that no seed here repeats it.
    secrets = {
        v
        for v in _content(real)
        if v not in kept and v != 0 and v != "" and not (isinstance(v, str) and DAY.fullmatch(v))
    }
    assert len(secrets) >= 25, "the fixture holds enough values to mean something"

    for seed in range(40):
        fake = _copy(real, seed=seed)
        leaked = secrets & set(_content(fake))
        assert not leaked, f"seed {seed}: the copy still holds {sorted(map(str, leaked))}"
        document = json.dumps(fake, ensure_ascii=False)
        for value in secrets:
            # A date is one of a few hundred, so a day can sit inside another stamp by
            # chance; it is compared whole above, like every value.
            if isinstance(value, str) and len(value) >= 6 and not ISO.fullmatch(value):
                assert value not in document, f"seed {seed}: {value!r}"
        assert "ZXQ" not in document


def test_no_part_of_the_record_reaches_the_composed_page():
    real = _read()
    page = dashboard_frame.compose('<p data-dashboard-field="headline"></p>', _copy(real, seed=3))
    kept = dashboard_synth.fixed_vocabulary() | {"green", "amber", "red"}

    for value in _content(real):
        if isinstance(value, str) and len(value) >= 6 and value not in kept:
            assert value not in page, value
    assert "ZXQ" not in page and "KiroCrew/pull" not in page


def test_a_fresh_seed_is_used_every_time_and_it_is_not_taken_from_the_record(monkeypatch):
    assert not inspect.signature(dashboard_synth._fresh_rng).parameters
    asked: list[int] = []

    def entropy(count: int) -> bytes:
        asked.append(count)
        return bytes([len(asked)]) * count

    monkeypatch.setattr(dashboard_synth, "os", SimpleNamespace(urandom=entropy))

    first = dashboard_synth.synthesize(_read(), model=MODEL, page=PAGE, now=NOW)
    second = dashboard_synth.synthesize(_read(), model=MODEL, page=PAGE, now=NOW)

    assert asked == [32, 32], "each copy is seeded from the operating system's entropy"
    assert first != second
    assert _copy(seed=7) == _copy(seed=7), "a given generator is reproducible, which is for tests"
    assert _copy(seed=7) != _copy(seed=8)


def test_a_replacement_is_drawn_without_looking_at_the_character_it_replaces():
    """If the real letter were left out of the draw, a page saved again and again would find it
    as the one never shown. Every letter and digit of the class turns up at every position,
    the real one included, and at about the same rate."""
    text = "PIN 4821 Bob"
    seen: list[dict[str, int]] = [dict() for _ in text]
    for seed in range(1500):
        fake = _copy(_read(only=text), seed=seed)["fields"]["only"]
        assert len(fake) == len(text)
        for place, char in enumerate(fake):
            seen[place][char] = seen[place].get(char, 0) + 1

    for place, original in enumerate(text):
        if original == " ":
            assert seen[place] == {" ": 1500}
            continue
        counts = seen[place]
        alphabet = (
            "0123456789"
            if original.isdigit()
            else (
                "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if original.isupper() else "abcdefghijklmnopqrstuvwxyz"
            )
        )
        assert set(counts) == set(alphabet), (place, original)
        expected = 1500 / len(alphabet)
        assert counts[original] > expected / 3, (place, original, counts[original])


def test_a_number_is_drawn_without_looking_at_the_number_it_replaces():
    one_digit = {_copy(_read(only=7), seed=seed)["fields"]["only"] for seed in range(300)}
    assert one_digit == set(range(1, 10)), "the real 7 turns up among the draws"
    text = {_copy(_read(only="5"), seed=seed)["fields"]["only"] for seed in range(300)}
    assert text == {str(n) for n in range(1, 10)}
    five_digits = {_copy(_read(only=48213), seed=seed)["fields"]["only"] for seed in range(300)}
    assert all(10_000 <= n < 100_000 for n in five_digits) and len(five_digits) > 250


def test_two_different_values_never_become_the_same_one():
    real = _read(a="5", b="6", c="7", d=11, e=12)
    for seed in range(200):
        fields = _copy(real, seed=seed)["fields"]
        assert len({fields["a"], fields["b"], fields["c"]}) == 3
        assert fields["d"] != fields["e"]


def test_small_distinct_numbers_and_codes_never_merge():
    """The nine one-digit numbers, the ninety two-digit ones and every single letter fill
    their whole domain, so each real value needs a replacement no other value has."""
    ones = list(range(1, 10))
    twos = list(range(10, 100))
    letters = [chr(c) for c in range(ord("a"), ord("z") + 1)]
    reals = [float(n) / 10 for n in range(11, 100) if n % 10]
    for seed in range(60):
        fields = _copy(_read(ones=ones, twos=twos, letters=letters, reals=reals), seed=seed)[
            "fields"
        ]
        assert sorted(fields["ones"]) == ones, seed
        assert len(set(fields["twos"])) == 90 and len(set(fields["letters"])) == 26, seed
        assert len(set(fields["reals"])) == len(reals), seed


def test_equal_numbers_are_one_number_whatever_their_type():
    """``101`` and ``101.0`` are the same number to a page, so a join on one finds the other."""
    fields = _copy(_read(a=101, b=101.0, c=7200, d=7200.0, e=-3.5, f=-3.5))["fields"]

    assert fields["a"] == fields["b"] and fields["c"] == fields["d"] and fields["e"] == fields["f"]
    assert isinstance(fields["a"], int) and isinstance(fields["b"], float)
    assert isinstance(fields["c"], int) and isinstance(fields["d"], float)


def test_every_number_of_a_size_is_given_out_once_in_one_draw_each():
    """A whole domain (the 9000 numbers of four digits) is shared out without a repeat, and
    the work stays one draw per number however full the domain gets."""

    class Counting(random.Random):
        draws = 0

        def randrange(self, *args: Any, **kwargs: Any) -> int:
            type(self).draws += 1
            return super().randrange(*args, **kwargs)

    numbers = list(range(1000, 10000))
    real = _read(nums=numbers, again=[float(n) for n in numbers])
    distinct = {
        n
        for n in _content(real)
        if isinstance(n, (int, float)) and not isinstance(n, bool) and n != 0
    }

    fields = dashboard_synth.synthesize(real, rng=Counting(5), now=NOW, page="")["fields"]

    assert sorted(fields["nums"]) == numbers, "each of the 9000 slots is given out exactly once"
    assert fields["again"] == [float(n) for n in fields["nums"]]
    assert Counting.draws == len(distinct), "one draw per distinct number"


def test_every_fraction_of_a_size_is_given_out_once():
    tenths = [n / 10 for n in range(1, 10)]
    hundredths = [n / 100 for n in range(11, 100) if n % 10]

    fields = _copy(_read(t=tenths, h=hundredths), seed=3)["fields"]

    assert sorted(fields["t"]) == tenths
    assert sorted(fields["h"]) == sorted(hundredths)


def test_a_number_that_is_a_key_is_replaced():
    real = _read(by_turn={4821: "a", 90817: "b"})

    fake = _copy(real, page="fields.by_turn")["fields"]["by_turn"]

    assert len(fake) == 2 and not set(fake) & {4821, 90817}


def test_eight_distinct_symbols_stay_eight_and_a_map_keyed_by_them_is_copied():
    marks = [chr(c) for c in range(0x1F44D, 0x1F44D + 8)]
    real = _read(marks=marks, tally={mark: n for n, mark in enumerate(marks, start=1)})

    for seed in range(40):
        fields = _copy(real, seed=seed)["fields"]
        assert len(set(fields["marks"])) == 8, seed
        assert len(fields["tally"]) == 8, seed


def test_a_uuid_key_is_an_id_even_when_the_page_spells_it():
    key = "123e4567-e89b-12d3-a456-426614174000"
    real = _read(doc={key: {"total": 3}, "summary": 1})
    model = {"types": {"doc": {"type": "object", "source": {"agentic": True}}}}

    fake = _copy(real, model=model, page=f"fields.doc['{key}'].total; fields.doc.summary")

    assert key not in json.dumps(fake) and "summary" in fake["fields"]["doc"]


def test_invisible_and_private_use_characters_are_not_carried_over():
    """Tag characters spell ASCII that cannot be seen; the copy holds none of them."""
    hidden = "".join(chr(0xE0000 + ord(c)) for c in "pin 4321")
    text = "secret" + hidden + "\ue000\u200d"

    fake = _copy(_read(note=text), seed=2)["fields"]["note"]
    only_hidden = _copy(_read(note=hidden), seed=2)["fields"]["note"]

    assert len(fake) == len(text) and len(only_hidden) == len(hidden)
    for char in fake + only_hidden:
        assert unicodedata.category(char)[0] not in "C", hex(ord(char))


def test_text_with_nothing_to_replace_is_left_as_it_is():
    fields = _copy(_read(dash="--", spaces="  ", marks="(...)"))["fields"]

    assert (fields["dash"], fields["spaces"], fields["marks"]) == ("--", "  ", "(...)")


def test_the_record_itself_is_not_changed():
    real = _read()
    before = copy.deepcopy(real)

    _copy(real)

    assert real == before


def test_a_key_the_read_gains_later_is_copied_as_a_value():
    real = _read()
    real["extra"] = {"note": "Confidential ZXQ roadmap"}

    fake = _copy(real)

    assert "note" not in fake["extra"], "a key the page does not name is invented"
    assert "Confidential" not in json.dumps(fake) and "roadmap" not in json.dumps(fake)


def test_a_type_json_cannot_carry_becomes_null():
    real = _read(thing=object(), stamp_object=complex(1, 2))

    fake = _copy(real)

    assert fake["fields"]["thing"] is None and fake["fields"]["stamp_object"] is None


# --------------------------------------------------------------------------- #
# 3. What is kept so the page takes the same path
# --------------------------------------------------------------------------- #


def test_the_words_of_a_closed_vocabulary_are_kept():
    fields = _copy()["fields"]

    assert fields["state"] == "accepted"
    assert [row["state"] for row in fields["items"]] == ["accepted", "open"]
    assert fields["items"][1]["verdict"] == "pass"


def test_an_enum_field_shows_one_of_its_labels_and_never_the_value_the_package_guessed():
    """The labels are the package's own words, so keeping the real value when it is one of
    them would let a package test its guesses against the record. The field shows a label
    drawn without looking at the value."""
    labels = ["green", "amber", "red"]
    model = {
        "types": {
            "tone": {"type": "enum", "choices": labels, "source": {"agentic": True}},
            "goal": {
                "type": "enum",
                "choices": ["Acquire Acme", "Divest Acme"],
                "source": {"fold": "ledger", "path": "goal"},
            },
        }
    }
    shown_tone: set[str] = set()
    for seed in range(60):
        fields = _copy(_read(tone="green", goal="Acquire Acme"), model=model, seed=seed)["fields"]
        shown_tone.add(fields["tone"])
        assert fields["tone"] in labels and fields["goal"] in ("Acquire Acme", "Divest Acme")
    assert shown_tone == set(labels), "every label turns up, the real one among them"
    # A value the package lists is drawn for, and one it does not list is, too.
    other = _copy(_read(tone="turquoise"), model=model)["fields"]["tone"]
    assert other in labels
    assert _copy(_read(tone=None), model=model)["fields"]["tone"] is None
    assert _copy(_read(tone=""), model=model)["fields"]["tone"] == ""


def test_an_enum_label_is_not_a_word_the_rest_of_the_record_keeps():
    model = {
        "types": {
            "rows": {
                "type": "enum",
                "choices": ["Project Atlas", "Fire Alice"],
                "source": {"fold": "work", "path": "items"},
            }
        }
    }
    real = _read(rows={"Project Atlas": ["Fire Alice", "x"]})

    fake = _copy(real, model=model, page="")["fields"]["rows"]

    assert "Project Atlas" not in json.dumps(fake) and "Fire Alice" not in json.dumps(fake)


def test_the_vocabulary_is_read_from_the_crew_log_schema():
    words = dashboard_synth.fixed_vocabulary()

    assert {"accepted", "open", "pass", "done", "blocked", "question"} <= words
    assert dashboard_synth.fixed_vocabulary() is words, "built once"
    assert "ZXQ" not in words


def test_equal_values_stay_equal_and_different_ones_stay_different():
    fields = _copy()["fields"]
    rows = fields["items"]

    assert fields["who"] == rows[0]["item_id"] == rows[0]["parent_item"] == rows[1]["parent_item"]
    assert rows[0]["item_id"] != rows[1]["item_id"]
    assert fields["open_prs"] != fields["items"][0]["pr"]


def test_distinct_values_never_become_one():
    many = [f"row-{n:04d}-ZXQ" for n in range(300)]
    real = _read(many=many, counts=list(range(1000, 1300)))

    fields = _copy(real)["fields"]

    assert len(set(fields["many"])) == 300
    assert len(set(fields["counts"])) == 300


# --------------------------------------------------------------------------- #
# 4. Keys
# --------------------------------------------------------------------------- #


def test_the_fields_a_read_carries_and_the_keys_the_page_names_are_kept():
    fake = _copy()

    assert list(fake["fields"]) == list(FIELDS)
    assert list(fake["fields"]["items"][0]) == list(FIELDS["items"][0])
    assert fake["agentic"] == ["headline", "tone"] and fake["missing"] == ["nothing"]
    assert fake["locale"] == "en" and fake["subject"] == "crewmate" and "stale" not in fake
    assert list(fake["written_at"]) == ["headline", "tone"]


def test_a_key_the_page_does_not_name_is_invented_and_the_rows_keep_their_size():
    """Without a page naming them, a row's keys are the record's text."""
    fake = _copy(page="")["fields"]["items"]

    for row, real in zip(fake, FIELDS["items"]):
        # A key that is a word of a closed vocabulary, or a field the read declares, is
        # structure whatever the page says.
        assert len(row) == len(real)
        assert set(row) & set(real) <= dashboard_synth.fixed_vocabulary() | set(FIELDS)
    # The same key is the same invented key in every row, so the rows still line up.
    assert list(fake[0]) == list(fake[1])


def test_a_key_the_page_names_in_quotes_is_kept():
    """A key with a space or a dash is reached as a quoted string, so a quoted string counts."""
    real = _read(notes={"Q3 board deck": {"owner": "x"}, "other key": 1})

    fake = _copy(real, page="""fields.notes["Q3 board deck"].owner""")["fields"]["notes"]

    assert "Q3 board deck" in fake and "other key" not in fake and "owner" in fake["Q3 board deck"]
    assert len(fake) == 2


def test_the_words_a_page_spells_are_found_in_one_bounded_pass():
    names = dashboard_synth.names_in(
        "row.title; row['Q3 deck']; \"two words\"; `tick`; // not-a-word-5\nx_1 $y"
    )

    assert {"row", "title", "Q3 deck", "two words", "tick", "x_1", "$y", "not"} <= names
    assert "a b" not in names
    big = " ".join(f"w{n}" for n in range(dashboard_synth._MAX_NAMES + 50))
    assert len(dashboard_synth.names_in(big)) == dashboard_synth._MAX_NAMES


def test_the_keys_of_a_map_keyed_by_a_runtime_id_are_invented_even_when_the_page_names_them():
    real = _read()
    # The catalog names usage.by_model as a map keyed by a model id.
    from kiro_crew.dashboard_synth import _shape_at

    assert _shape_at({"fold": "usage", "path": "by_model"}) == {
        "type": "object",
        "opaque": True,
        "keyed_by": "a model id",
    }

    named = PAGE + ' fields.by_model["claude-opus-4-xyz"]'
    fake = _copy(real, page=named)["fields"]["by_model"]

    assert len(fake) == 2 and not set(fake) & set(FIELDS["by_model"])
    assert [v["turns"] for v in fake.values()] != [4821, 377]


def test_a_map_the_package_does_not_bind_to_a_fold_has_only_the_keys_the_page_names():
    named = _copy(model={"types": {}}, page=PAGE + ' by_model["claude-opus-4-xyz"]')
    unnamed = _copy(model={"types": {}})

    assert set(named["fields"]["by_model"]) == {"claude-opus-4-xyz", "gpt-9-secret"} - {
        "gpt-9-secret"
    } | {k for k in named["fields"]["by_model"] if k not in FIELDS["by_model"]}
    assert "claude-opus-4-xyz" in named["fields"]["by_model"]
    assert "gpt-9-secret" not in named["fields"]["by_model"]
    assert not set(unnamed["fields"]["by_model"]) & set(FIELDS["by_model"])
    assert named["fields"]["by_model"]["claude-opus-4-xyz"]["turns"] != 4821


@pytest.mark.parametrize("fold, path", [("panel", "data"), ("agentic", "fields")])
def test_a_map_the_owner_keyed_has_the_keys_the_page_names_and_invents_the_rest(fold, path):
    from kiro_crew.dashboard_synth import _shape_at

    node = _shape_at({"fold": fold, "path": path})
    assert node is not None and node["opaque"] is True
    assert any(node is owner for owner in dashboard_synth._owner_keyed())
    real = _read(doc={"Acme Corp renewal": {"Fire Alice": 3}, "summary": {"total": 12}})
    model = {"types": {"doc": {"type": "object", "source": {"fold": fold, "path": path}}}}

    fake = _copy(real, model=model, page="fields.doc.summary.total")["fields"]["doc"]

    assert set(fake) & {"summary"} == {"summary"} and "Acme Corp renewal" not in fake
    assert "Fire Alice" not in json.dumps(fake) and fake["summary"]["total"] != 12


def test_a_map_nested_in_a_row_has_its_keys_invented_even_when_the_page_names_them():
    """The catalog does not describe a list's rows, and some rows hold a map keyed by data (the
    sources of a context turn). A row's own keys follow the page; what is inside them is data."""
    real = _read(
        turns=[
            {"turn": 1, "tokens": 40, "sources": {"memory": 12, "chat": 30}},
            {"turn": 2, "tokens": 50, "sources": {"memory": 7}},
        ]
    )
    model = {
        "types": {"turns": {"type": "array", "source": {"fold": "usage", "path": "context.turns"}}}
    }
    page = "fields.turns.map(t => t.turn + t.tokens + Object.keys(t.sources) + t.sources.memory)"

    fake = _copy(real, model=model, page=page)["fields"]["turns"]

    assert [set(row) for row in fake] == [{"turn", "tokens", "sources"}] * 2
    assert "memory" not in json.dumps(fake) and "chat" not in json.dumps(fake)
    assert [len(row["sources"]) for row in fake] == [2, 1]
    assert set(fake[1]["sources"]) <= set(fake[0]["sources"]), "one source, one invented key"


def test_an_owner_document_keeps_the_keys_the_page_names_at_every_depth():
    real = _read(notes={"todo": [{"name": "x", "tags": {"urgent": 1, "Q3 deck": 2}}]})
    model = {"types": {"notes": {"type": "object", "source": {"agentic": True}}}}
    page = "fields.notes.todo.map(t => t.name + t.tags.urgent)"

    fake = _copy(real, model=model, page=page)["fields"]["notes"]

    row = fake["todo"][0]
    assert set(row) == {"name", "tags"} and "urgent" in row["tags"]
    assert "Q3 deck" not in json.dumps(fake) and len(row["tags"]) == 2


@pytest.mark.parametrize("path", ["data", "data.report"])
def test_a_field_at_or_below_a_panel_document_is_an_owner_document(path):
    from kiro_crew.dashboard_synth import _walk_source

    node, owner = _walk_source({"fold": "panel", "path": path})

    assert owner is True
    assert (node is not None) == (path == "data")
    assert _walk_source({"fold": "usage", "path": "by_model"})[1] is False
    assert _walk_source({"agentic": True}) == (None, True)


def test_every_opaque_node_is_either_runtime_keyed_or_listed_as_owner_keyed():
    """A node added to the catalog is invented unless it is listed, which is the safe side."""
    from kiro_crew.dashboard_synth import _fold_shapes

    found = []

    def walk(node, where):
        if node.get("opaque"):
            found.append((where, node.get("keyed_by")))
        for name, inner in (node.get("properties") or {}).items():
            walk(inner, where + (name,))

    for fold, shape in _fold_shapes().items():
        walk(shape, (fold,))

    owner = {("panel", "data"), ("agentic", "fields")}
    assert owner <= {where for where, _ in found}
    assert all(keyed for _, keyed in found), "an opaque node says who keys it"


def test_an_agentic_value_has_the_keys_the_page_names_and_invents_the_rest():
    real = _read(
        notes={
            "Acme Corp renewal": {"Q3 board deck": "late"},
            "todo": [{"name": "x", "done": True}],
        }
    )
    model = {"types": {"notes": {"type": "object", "source": {"agentic": True}}}}

    fake = _copy(real, model=model, page="fields.notes.todo.map(t => t.name + t.done)")["fields"][
        "notes"
    ]

    assert "todo" in fake and fake["todo"][0].keys() == {"name", "done"}
    assert "Acme Corp renewal" not in json.dumps(fake) and "Q3 board deck" not in json.dumps(fake)
    assert len(fake) == 2


def test_a_key_that_is_a_field_of_a_known_shape_stays():
    real = _read(totals={"spawned": 4821, "completed": 3921})
    model = {
        "types": {"totals": {"type": "object", "source": {"fold": "subagents", "path": "totals"}}}
    }

    fake = _copy(real, model=model)["fields"]["totals"]

    assert list(fake) == ["spawned", "completed"] and fake["spawned"] != 4821


# --------------------------------------------------------------------------- #
# 5. Bounds
# --------------------------------------------------------------------------- #


def test_the_copy_is_no_larger_than_the_record():
    real = _read()

    assert len(json.dumps(_copy(real))) <= len(json.dumps(real)) + 64


def test_a_read_with_too_many_values_is_refused(monkeypatch):
    monkeypatch.setattr(dashboard_synth, "MAX_NODES", 50)

    with pytest.raises(dashboard_synth.SynthesisRefused):
        _copy()


def test_a_read_with_too_much_text_is_refused(monkeypatch):
    monkeypatch.setattr(dashboard_synth, "MAX_CHARS", 100)

    with pytest.raises(dashboard_synth.SynthesisRefused):
        _copy()


def test_a_read_nested_too_deeply_is_refused():
    deep: Any = "leaf"
    for _ in range(dashboard_synth.MAX_DEPTH + 2):
        deep = {"k": deep}

    with pytest.raises(dashboard_synth.SynthesisRefused):
        _copy(_read(deep=deep))


def test_something_that_is_not_a_read_is_refused():
    with pytest.raises(dashboard_synth.SynthesisRefused):
        dashboard_synth.synthesize(["not", "an", "object"])  # type: ignore[arg-type]


def test_the_work_grows_in_step_with_the_record(monkeypatch):
    """One walk to learn the values and one to build the copy, each over each value once."""
    from kiro_crew.dashboard_synth import _Synth

    visits = []
    original = _Synth._value

    def counting(self, value, shape, context):
        visits.append(1)
        return original(self, value, shape, context)

    small = _read(rows=[{"n": 10_001 + i, "t": f"text-{i}"} for i in range(10)])
    large = _read(rows=[{"n": 10_001 + i, "t": f"text-{i}"} for i in range(100)])
    monkeypatch.setattr(_Synth, "_value", counting)
    _copy(small)
    few = len(visits)
    visits.clear()
    _copy(large)
    many = len(visits)

    # 90 more rows of 3 values each (the row and its two members), no more.
    assert many - few == 90 * 3
