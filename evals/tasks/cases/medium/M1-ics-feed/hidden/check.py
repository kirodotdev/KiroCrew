"""Hidden check for M1: cancelled events are dropped, and the cap keeps the nearest events."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4] / "lib"))
from hidden import main  # noqa: E402

# The required cap, fixed here: the submission's own constant is not trusted.
MAX_CALENDAR_EVENTS = 500


def _helpers():
    """Feed builders, kept here so an edit to the visible tests cannot change them."""
    from datetime import datetime, timedelta, timezone

    def stamp(hours_ahead):
        at = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
        return at.strftime("%Y%m%dT%H%M%SZ")

    def vevent(**props):
        lines = ["BEGIN:VEVENT"] + [f"{k}:{v}" for k, v in props.items()]
        return "\n".join(lines + ["END:VEVENT"])

    def ics(*events):
        return "\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", *events, "END:VCALENDAR"])

    return stamp, vevent, ics


def test_a_cancelled_event_is_not_listed():
    from ics_feed import parse_ics

    stamp, vevent, ics = _helpers()
    text = ics(
        vevent(UID="c", SUMMARY="Cancelled review", DTSTART=stamp(2), STATUS="CANCELLED"),
        vevent(UID="k", SUMMARY="Kept", DTSTART=stamp(3)),
    )
    assert [e.title for e in parse_ics(text)] == ["Kept"]


def test_cancelled_is_read_trimmed_and_in_any_case():
    from ics_feed import parse_ics

    stamp, vevent, ics = _helpers()
    text = ics(vevent(UID="c", SUMMARY="x", DTSTART=stamp(2), STATUS=" cancelled "))
    assert parse_ics(text) == []


def test_tentative_and_confirmed_events_stay_listed():
    from ics_feed import parse_ics

    stamp, vevent, ics = _helpers()
    text = ics(
        vevent(UID="t", SUMMARY="Maybe", DTSTART=stamp(1), STATUS="TENTATIVE"),
        vevent(UID="f", SUMMARY="Sure", DTSTART=stamp(2), STATUS="CONFIRMED"),
    )
    assert [e.title for e in parse_ics(text)] == ["Maybe", "Sure"]


def test_the_cap_keeps_the_nearest_events_whatever_the_feed_order():
    from ics_feed import parse_ics

    stamp, vevent, ics = _helpers()
    later = [
        vevent(UID=f"l{i}", SUMMARY=f"L{i}", DTSTART=stamp(24 * 6 + i / 60))
        for i in range(MAX_CALENDAR_EVENTS)
    ]
    text = ics(*later, vevent(UID="n", SUMMARY="Nearest meeting", DTSTART=stamp(0.1)))
    titles = [e.title for e in parse_ics(text, days=7)]
    assert len(titles) == MAX_CALENDAR_EVENTS, len(titles)
    assert titles[0] == "Nearest meeting", titles[:3]
    assert titles[-1] == f"L{MAX_CALENDAR_EVENTS - 2}", titles[-3:]


def test_a_reversed_feed_over_the_cap_returns_the_nearest_in_order():
    from ics_feed import parse_ics

    stamp, vevent, ics = _helpers()
    total = MAX_CALENDAR_EVENTS + 20
    text = ics(
        *[
            vevent(UID=f"u{i}", SUMMARY=f"S{i}", DTSTART=stamp(1 + i / 60))
            for i in reversed(range(total))
        ]
    )
    assert [e.title for e in parse_ics(text)] == [f"S{i}" for i in range(MAX_CALENDAR_EVENTS)]


def test_the_visible_tests_still_pass():
    import test_ics_feed as visible

    for name in dir(visible):
        if name.startswith("test_"):
            getattr(visible, name)()


if __name__ == "__main__":
    main(globals())
