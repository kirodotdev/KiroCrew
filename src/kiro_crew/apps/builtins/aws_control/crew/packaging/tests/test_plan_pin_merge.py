"""An unselected plan must not replace a reviewed content pin.

The signature rule and the pin merge were each defensible alone and wrong
together. `merge_plans` refuses an UNSIGNED plan only when it selects something,
which is right on its own terms: a plan selecting nothing approves nothing. But
the merge then took that plan's pins anyway, last writer winning, so an unsigned
plan that selected nothing could replace the content hash a SIGNED plan had been
reviewed against. Verification compares against the merged pin, so the build would
then accept and ship content no reviewer ever saw.

Two rules close it: a pin is only taken from a plan that SELECTS the item, and two
selecting plans that disagree are refused rather than ordered.
"""

from __future__ import annotations

import json

import pytest

from .test_producer import load_build


def mod_plan_version():
    """Read the version from the module rather than hardcoding 1."""
    return load_build().PLAN_VERSION


def _write_plan(path, crew: str, *, selections, pins, signed: bool):
    doc = {
        "plan_version": mod_plan_version(),
        "crew": crew,
        "reviewed_by": "a reviewer" if signed else "",
        "reviewed_at": "2026-09-04" if signed else "",
        "skills": [
            {"id": cid, "include": on, "sha256": pins.get(cid, "")}
            for cid, on in selections.items()
        ],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def _pins_of(plan, kind="skills"):
    return dict(plan.pins.get(kind, {}))


def test_an_unselected_unsigned_plan_cannot_replace_a_reviewed_pin(tmp_path):
    mod = load_build()
    approved = _write_plan(
        tmp_path / "approved.json",
        "frontdesk",
        selections={"faq": True},
        pins={"faq": "H1"},
        signed=True,
    )
    # Selects nothing, so the unsigned refusal does not fire, and it has no
    # business pinning anything either.
    drive_by = _write_plan(
        tmp_path / "driveby.json",
        "frontdesk",
        selections={"faq": False},
        pins={"faq": "H2"},
        signed=False,
    )

    merged = mod.merge_plans([approved, drive_by], "frontdesk")

    assert merged is not None
    assert merged.selections["skills"]["faq"] is True, "the approval was lost"
    assert _pins_of(merged) == {"faq": "H1"}, (
        "an unsigned plan that selected nothing replaced the reviewed pin: " f"{_pins_of(merged)}"
    )


def test_order_does_not_decide_the_pin(tmp_path):
    """The same two plans the other way round must give the same answer."""
    mod = load_build()
    approved = _write_plan(
        tmp_path / "approved.json",
        "frontdesk",
        selections={"faq": True},
        pins={"faq": "H1"},
        signed=True,
    )
    drive_by = _write_plan(
        tmp_path / "driveby.json",
        "frontdesk",
        selections={"faq": False},
        pins={"faq": "H2"},
        signed=False,
    )

    assert _pins_of(mod.merge_plans([drive_by, approved], "frontdesk")) == {"faq": "H1"}


def test_two_selecting_plans_that_disagree_are_refused(tmp_path):
    """Not resolved by ordering: one of the two reviewers approved something else."""
    mod = load_build()
    a = _write_plan(
        tmp_path / "a.json",
        "frontdesk",
        selections={"faq": True},
        pins={"faq": "H1"},
        signed=True,
    )
    b = _write_plan(
        tmp_path / "b.json",
        "frontdesk",
        selections={"faq": True},
        pins={"faq": "H2"},
        signed=True,
    )

    with pytest.raises(mod.ExportRefused) as exc:
        mod.merge_plans([a, b], "frontdesk")
    assert "pin different content" in str(exc.value)


def test_two_selecting_plans_that_agree_are_fine(tmp_path):
    """The guard must not refuse the ordinary case of two plans in step."""
    mod = load_build()
    a = _write_plan(
        tmp_path / "a.json",
        "frontdesk",
        selections={"faq": True},
        pins={"faq": "H1"},
        signed=True,
    )
    b = _write_plan(
        tmp_path / "b.json",
        "frontdesk",
        selections={"faq": True},
        pins={"faq": "H1"},
        signed=True,
    )

    assert _pins_of(mod.merge_plans([a, b], "frontdesk")) == {"faq": "H1"}


def test_MUTATION_pin_taken_from_a_non_selecting_plan(tmp_path):
    """Put the old merge back and the unreviewed pin wins again.

    Both guards have to come out, and that is worth knowing: with only the
    selection guard removed the CONFLICT guard catches it instead, so the two
    overlap rather than each covering a separate case. The mutation therefore
    restores the original single line, which is what the code actually was.
    """
    approved = _write_plan(
        tmp_path / "approved.json",
        "frontdesk",
        selections={"faq": True},
        pins={"faq": "H1"},
        signed=True,
    )
    drive_by = _write_plan(
        tmp_path / "driveby.json",
        "frontdesk",
        selections={"faq": False},
        pins={"faq": "H2"},
        signed=False,
    )

    bad = load_build(
        mutate=(
            "                if not pin:\n                    continue",
            "                if pin:\n                    merged_pins[kind][cid] = pin\n"
            "                if True:\n                    continue",
        )
    )
    merged = bad.merge_plans([approved, drive_by], "frontdesk")
    assert _pins_of(merged) == {
        "faq": "H2"
    }, "mutation did not take effect; this test proves nothing"
