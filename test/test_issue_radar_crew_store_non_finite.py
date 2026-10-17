"""A non-finite or fractional number never becomes a value the crew store holds.

This file pins the numeric coercions the store makes on settings, crew records and work
items: ``Infinity``, ``NaN`` and an overflowing ``1e309``, read from a file or sent in a
patch, end as the default, the value already stored or ``None``, so none reaches a
payload a strict JSON parser refuses; a fractional TTL is refused rather than truncated
while an integral float is accepted; and a legitimate number still round-trips. The
crew records and settings themselves live in ``test_issue_radar_crew_store.py``.
"""

import json
import math

import pytest
from issue_radar_crew_store_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    OWNER,
    REPO,
    _crew,
    _entries,
    _isolated_crew_log,
    _item,
    _live_crew,
    _unit,
)

from kiro_crew.apps.builtins.issue_radar.backend import crew_store as cs

# ── non-finite numbers ──────────────────────────────────────────────────────
#
# Python's `json` decodes `Infinity`, `-Infinity` and `NaN` by default, and it
# also produces them from a literal that looks ordinary: `1e309` overflows to
# `inf` SILENTLY. `int()` accepts neither (`OverflowError` / `ValueError`), so
# every numeric coercion in this store is a crash the store's contract does not
# allow — a malformed STORED value must read as the default.
#
# Storing one instead of crashing would be worse, not better, which is why none of
# these tests asserts a clamp: `json.dumps` writes a bare `Infinity`, which is not
# JSON, so a single poisoned record makes the whole payload unparseable for the
# dashboard — and a bound compared against it (`open_count >= inf`) is simply False
# for every count, so the cap disappears without raising anything.

#: Every literal a JSON body or a stored file can carry that decodes non-finite.
#: `1e309` is here because it is the one that arrives without anybody writing
#: `Infinity`: it is the shape a real overflow takes.
NON_FINITE_LITERALS = ("1e309", "-1e309", "Infinity", "-Infinity", "NaN")


@pytest.mark.parametrize("literal", ["47.9", "0.5", "-1.5", "2.000001"])
def test_a_fractional_ttl_is_refused_rather_than_truncated(tmp_path, literal):
    """A value the operator never asked for must not be stored silently.

    REGRESSION: ``_finite_int`` ended in a bare ``int(value)``, which TRUNCATES, so
    ``47.9`` stored as ``47`` and the form reported success. That is the same silent
    substitution the frontend's ``Number.isInteger`` guard refuses one layer up —
    refusing in both places means neither can invent a value on its own.

    Asserted through the PATCH path: the previous value has to stand, exactly as it
    does for a non-finite number or an over-long ``commit_trailer``.
    """
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 48}, tmp_path)

    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": float(literal)}, tmp_path)

    got = cs.read_settings(OWNER, REPO, tmp_path)
    assert got["claim_ttl_hours"] == 48, "a fractional TTL was truncated into the record"


def test_an_integral_float_is_still_accepted(tmp_path):
    """``48.0`` carries no fraction, so refusing it would reject valid JSON.

    The bound is "not an integer", not "not an int": a JSON number round-tripped
    through a float is the ordinary shape a browser sends.
    """
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 12}, tmp_path)

    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 36.0}, tmp_path)

    got = cs.read_settings(OWNER, REPO, tmp_path)
    assert got["claim_ttl_hours"] == 36, "an integral float was refused"


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_stored_ttl_reads_as_the_default(tmp_path, literal):
    """The store's contract: a malformed stored value reads as the default.

    Reachable without a request — a settings file is an ordinary JSON file in the
    data home, so it can be hand-edited or restored from a backup.
    """
    path = cs.settings_path(OWNER, REPO, tmp_path)
    path.write_text('{"schema": 1, "claim_ttl_hours": %s}' % literal)
    assert json.loads(path.read_text())["claim_ttl_hours"] != 48, "literal decoded finite"

    got = cs.read_settings(OWNER, REPO, tmp_path)

    assert got["claim_ttl_hours"] == cs.DEFAULT_SETTINGS["claim_ttl_hours"]


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_ttl_patch_leaves_the_stored_value_alone(tmp_path, literal):
    """Same discipline as an over-long ``commit_trailer``: the value is dropped and
    the previous one stands, rather than the write raising."""
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 12}, tmp_path)

    stored = cs.write_settings(OWNER, REPO, {"claim_ttl_hours": json.loads(literal)}, tmp_path)

    assert stored["claim_ttl_hours"] == 12
    assert cs.read_settings(OWNER, REPO, tmp_path)["claim_ttl_hours"] == 12
    # And the file it wrote is still JSON — `json.dumps` would have emitted a bare
    # `Infinity`, which no strict parser reads.
    _assert_strict_json(cs.settings_path(OWNER, REPO, tmp_path))


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_max_open_is_ignored_like_any_out_of_range_value(tmp_path, literal):
    crew = _crew(tmp_path)
    updated = cs.update_crew(OWNER, REPO, crew["id"], {"max_open": json.loads(literal)}, tmp_path)
    assert updated["max_open"] == cs._DEFAULT_CREW["max_open"]


@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_avatar_variant_stores_as_none(tmp_path, literal):
    """``None`` is what this field already stores for a non-number, so a non-finite
    number joins that case rather than getting a rule of its own."""
    crew = _crew(tmp_path, avatar_variant=json.loads(literal))
    assert crew["avatar_variant"] is None
    updated = cs.update_crew(
        OWNER, REPO, crew["id"], {"avatar_variant": json.loads(literal)}, tmp_path
    )
    assert updated["avatar_variant"] is None


@pytest.mark.parametrize("field", ("pr_number", "claim_comment_id"))
@pytest.mark.parametrize("literal", NON_FINITE_LITERALS)
def test_a_non_finite_work_item_number_records_as_none(tmp_path, field, literal):
    crew, sid = _live_crew(tmp_path)
    item = _item(tmp_path, crew["id"], sid, 2251, {field: json.loads(literal)})
    assert item[field] is None
    # Left OUT of the entry rather than written as null: the line is JSON a strict
    # reader parses, and an absent field reads as "unchanged" to the fold.
    (entry,) = _entries(sid)
    assert field not in entry.data


def test_a_hand_edited_crew_record_cannot_defeat_the_slot_cap(tmp_path):
    """The read side, and the reason it matters more than the write side.

    Nothing in the app COMPARES against ``max_open`` with an exception to raise:
    the crew's brief renders it as prose and the page tests ``open >= max_open``,
    which is False for every count once the value is ``inf``. So an unchecked read
    does not crash — it silently removes the cap.
    """
    crew = _crew(tmp_path)
    _poison(cs.crew_path(OWNER, REPO, crew["id"], tmp_path), "max_open", "1e309")

    got = cs.read_crew(OWNER, REPO, crew["id"], tmp_path)

    assert got is not None
    assert got["max_open"] == cs._DEFAULT_CREW["max_open"]
    assert 99 >= got["max_open"], "the cap must be a number a count can exceed"
    assert [c["max_open"] for c in cs.list_crews(OWNER, REPO, tmp_path)] == [
        cs._DEFAULT_CREW["max_open"]
    ]


def test_a_hand_edited_crew_record_stays_serialisable(tmp_path):
    """One poisoned record must not take the page down for every crew: ``GET /crews``
    returns them all in one body, and a bare ``Infinity`` in it is not JSON."""
    crew = _crew(tmp_path)
    _poison(cs.crew_path(OWNER, REPO, crew["id"], tmp_path), "avatar_variant", "NaN")

    got = cs.read_crew(OWNER, REPO, crew["id"], tmp_path)

    assert got is not None
    assert got["avatar_variant"] is None
    # Strict, because `json.loads` would accept the `NaN` this asserts is gone.
    json.loads(json.dumps(got), parse_constant=_reject_constant)


def test_legitimate_numbers_still_round_trip(tmp_path):
    """The guard must not cost a valid value. Every field hardened above, with a
    number a real caller sends."""
    cs.write_settings(OWNER, REPO, {"claim_ttl_hours": 72}, tmp_path)
    assert cs.read_settings(OWNER, REPO, tmp_path)["claim_ttl_hours"] == 72

    crew = _crew(tmp_path, max_open=5, avatar_variant=2)
    assert (crew["max_open"], crew["avatar_variant"]) == (5, 2)
    reread = cs.read_crew(OWNER, REPO, crew["id"], tmp_path)
    assert reread is not None
    assert (reread["max_open"], reread["avatar_variant"]) == (5, 2)

    sid = _unit(crew["id"])
    item = _item(tmp_path, crew["id"], sid, 2251, {"pr_number": 2271, "claim_comment_id": 9911})
    assert (item["pr_number"], item["claim_comment_id"]) == (2271, 9911)
    # A later write must not blank a field it was not given.
    carried = _item(tmp_path, crew["id"], sid, 2251, {"next": "rebase"})
    assert (carried["pr_number"], carried["claim_comment_id"]) == (2271, 9911)
    # The bound still rejects a finite out-of-range value, unchanged.
    assert cs.update_crew(OWNER, REPO, crew["id"], {"max_open": 21}, tmp_path)["max_open"] == 5


def _reject_constant(name: str):
    raise AssertionError(f"non-finite constant {name} survived into the payload")


def _poison(path, field: str, literal: str) -> None:
    """Rewrite *field* in the JSON at *path* to the raw non-finite *literal*.

    Through the FILE, because that is the only way such a value gets onto a record:
    nothing in the store can write one. Through a sentinel rather than a textual
    substitution on the old value, so the edit cannot land on another field that
    happens to hold the same digits.
    """
    raw = {**json.loads(path.read_text()), field: "__poison__"}
    path.write_text(json.dumps(raw).replace('"__poison__"', literal))
    assert not math.isfinite(json.loads(path.read_text())[field]), "fixture is finite"


def _assert_strict_json(path) -> None:
    """The file parses under a decoder that refuses ``Infinity``/``NaN``.

    ``json.loads`` ACCEPTS all three by default, so a plain re-read would pass on a
    file no strict parser — including the dashboard's ``JSON.parse`` — can read.
    """
    json.loads(path.read_text(), parse_constant=_reject_constant)
