# testpaths-ok: standalone tests of this small repo, run as a script
from datetime import datetime, timedelta, timezone

from ics_feed import parse_ics


def stamp(hours_ahead: float) -> str:
    at = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
    return at.strftime("%Y%m%dT%H%M%SZ")


def vevent(**props: str) -> str:
    lines = ["BEGIN:VEVENT"] + [f"{k.replace('_', '-')}:{v}" for k, v in props.items()]
    return "\n".join(lines + ["END:VEVENT"])


def ics(*events: str) -> str:
    return "\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", *events, "END:VCALENDAR"])


def test_an_upcoming_event_is_listed():
    events = parse_ics(ics(vevent(UID="a", SUMMARY="Standup", DTSTART=stamp(2))))
    assert [e.title for e in events] == ["Standup"]


def test_events_come_back_sorted_by_start():
    text = ics(
        vevent(UID="b", SUMMARY="Later", DTSTART=stamp(5)),
        vevent(UID="a", SUMMARY="Sooner", DTSTART=stamp(1)),
    )
    assert [e.title for e in parse_ics(text)] == ["Sooner", "Later"]


def test_an_event_past_the_window_is_skipped():
    assert parse_ics(ics(vevent(UID="a", SUMMARY="Far", DTSTART=stamp(24 * 30)))) == []


def test_garbage_input_returns_empty():
    assert parse_ics("this is not a calendar") == []


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_"):
            _fn()
    print("ok")
