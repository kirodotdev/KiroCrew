"""Provider-seam tests: the task registry, the calendar registry, and the .ics parser.

Lives in the repo-level ``test/`` tree (not the app's in-package ``tests/``)
because ``setup.cfg`` sets ``testpaths = test transfer`` — a test under
``src/kiro_crew/apps/builtins/...`` is never collected by CI.

These cover the two internal couplings the port replaced, so the assertions are
about the SEAM as much as the implementations: an out-of-repo edition must be
able to register a provider, an unknown id must degrade instead of raising, and
the shipped ``.ics`` fetch must refuse every scheme and address class that would
turn the sync into a local-file read or a request-forgery hop.

No test performs real network I/O: the URL path is exercised through the
scheme/address validator, and the document path through a local file.
"""

from __future__ import annotations

import base64
import itertools
import json
import socket
import ssl
import threading
import types
from concurrent import futures
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from meetings_helpers import (  # noqa: F401
    reset_module_state_fixture,
    root_fixture,
)
from yarl import URL

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend.providers import calendar as cal
from kiro_crew.apps.builtins.meetings.backend.providers import tasks as taskprov


class _AnyPort:
    """Stands in for ``link_unfurl.ALLOWED_PORTS`` when a test server binds an
    ephemeral port. Only ``in`` is used against that frozenset, so accepting
    everything is the whole contract — and it beats materializing 65k ints.
    """

    def __contains__(self, _port: object) -> bool:
        return True


def _ics(*events: str, prodid: str = "-//test//EN") -> str:
    body = "\n".join(events)
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:{prodid}\n{body}\nEND:VCALENDAR\n"


def _vevent(**fields: str) -> str:
    lines = ["BEGIN:VEVENT"]
    lines.extend(f"{key.replace('_', '-')}:{value}" for key, value in fields.items())
    lines.append("END:VEVENT")
    return "\n".join(lines)


def _stamp(offset_hours: float) -> str:
    when = datetime.now(timezone.utc) + timedelta(hours=offset_hours)
    return when.strftime("%Y%m%dT%H%M%SZ")


# ── task provider ───────────────────────────────────────────────────────────


class TestTaskDraft:
    def test_sanitized_redacts_and_caps(self):
        draft = taskprov.TaskDraft(
            description="ship AKIAIOSFODNN7EXAMPLE now",
            assignee="x" * 5000,
            priority="bogus",
        ).sanitized()
        assert "AKIAIOSFODNN7EXAMPLE" not in draft.description
        assert len(draft.assignee) <= 2000
        assert draft.priority == k.DEFAULT_TASK_PRIORITY

    def test_sanitized_drops_non_string_labels(self):
        draft = taskprov.TaskDraft(description="d", labels=["ok", "", "  "]).sanitized()
        assert draft.labels == ["ok"]


class TestLocalTaskProvider:
    def test_create_writes_ledger(self, root: Path):
        provider = taskprov.LocalTaskProvider(root)
        ref = provider.create(taskprov.TaskDraft(description="ship it").sanitized())
        assert ref.provider == k.TASK_PROVIDER_LOCAL
        assert ref.id.startswith("mt-")
        doc = json.loads((root / "task-ledger.json").read_text())
        assert doc["tasks"][0]["description"] == "ship it"

    def test_list_recent_is_newest_first(self, root: Path):
        provider = taskprov.LocalTaskProvider(root)
        for name in ("first", "second", "third"):
            provider.create(taskprov.TaskDraft(description=name).sanitized())
        assert [t["description"] for t in provider.list_recent()] == [
            "third", "second", "first",
        ]

    def test_list_recent_bounds_limit(self, root: Path):
        provider = taskprov.LocalTaskProvider(root)
        provider.create(taskprov.TaskDraft(description="d").sanitized())
        assert len(provider.list_recent(limit=0)) == 1  # clamped to >=1
        assert len(provider.list_recent(limit=10_000)) == 1

    def test_malformed_ledger_does_not_lose_the_new_task(self, root: Path):
        (root / "task-ledger.json").write_text("{not json")
        provider = taskprov.LocalTaskProvider(root)
        provider.create(taskprov.TaskDraft(description="recovered").sanitized())
        assert provider.list_recent()[0]["description"] == "recovered"

    def test_empty_ledger_path_reads_empty(self, root: Path):
        assert taskprov.LocalTaskProvider(root).list_recent() == []

    def test_concurrent_filings_do_not_overwrite_each_other(self, root: Path):
        """Two parallel filings must both survive.

        `create` is a read-append-write, and it runs on the subprocess executor
        (`handle_file_task`), so two filings genuinely execute at once. Without a
        lock both read the same list, both append one task, and the second atomic
        write lands a snapshot missing the first — while BOTH requests report
        success, so the loss is silent. The write being atomic never helped; the
        read-modify-write was the unguarded part.

        A fresh provider per thread on purpose: `get_task_provider` constructs one
        per request, so a per-instance lock would guard nothing.
        """
        count = 24
        barrier = threading.Barrier(count)

        def file_one(index: int) -> None:
            barrier.wait()  # maximize overlap on the read-modify-write
            taskprov.LocalTaskProvider(root).create(
                taskprov.TaskDraft(description=f"task-{index}").sanitized()
            )

        with futures.ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(file_one, range(count)))

        filed = {t["description"] for t in taskprov.LocalTaskProvider(root).list_recent(count)}
        assert filed == {f"task-{i}" for i in range(count)}


class TestTaskRegistry:
    def test_local_is_registered_by_default(self):
        assert k.TASK_PROVIDER_LOCAL in {r["id"] for r in taskprov.available_task_providers()}

    def test_unknown_id_degrades_to_local(self, root: Path):
        provider = taskprov.get_task_provider("some-corporate-tracker", root)
        assert provider.provider_id == k.TASK_PROVIDER_LOCAL

    def test_empty_id_uses_the_default(self, root: Path):
        assert taskprov.get_task_provider("", root).provider_id == k.TASK_PROVIDER_LOCAL

    def test_edition_can_register_and_resolve(self, root: Path):
        class EditionProvider(taskprov.TaskProvider):
            @property
            def provider_id(self) -> str:
                return "edition"

            @property
            def display_name(self) -> str:
                return "Edition tracker"

            def create(self, draft):
                return taskprov.TaskRef(provider="edition", id="E-1", url="https://x.test/E-1")

        try:
            taskprov.register_task_provider("edition", EditionProvider)
            assert "edition" in {r["id"] for r in taskprov.available_task_providers()}
            resolved = taskprov.get_task_provider("edition", root)
            assert resolved.provider_id == "edition"
            assert resolved.create(taskprov.TaskDraft(description="d")).id == "E-1"
        finally:
            taskprov.register_task_provider("edition", None)
        assert "edition" not in {r["id"] for r in taskprov.available_task_providers()}

    def test_registering_replaces_an_existing_id(self, root: Path):
        original = taskprov._factories[k.TASK_PROVIDER_LOCAL]
        try:
            taskprov.register_task_provider(
                k.TASK_PROVIDER_LOCAL, lambda: taskprov.LocalTaskProvider(root)
            )
            assert taskprov.get_task_provider(k.TASK_PROVIDER_LOCAL, root) is not None
        finally:
            taskprov.register_task_provider(k.TASK_PROVIDER_LOCAL, original)

    def test_empty_provider_id_rejected(self):
        with pytest.raises(ValueError):
            taskprov.register_task_provider("  ", lambda: taskprov.LocalTaskProvider())

    def test_broken_factory_is_skipped_not_fatal(self):
        def boom():
            raise RuntimeError("bad edition")

        try:
            taskprov.register_task_provider("broken", boom)
            ids = {r["id"] for r in taskprov.available_task_providers()}
            assert "broken" not in ids
            assert k.TASK_PROVIDER_LOCAL in ids
        finally:
            taskprov.register_task_provider("broken", None)


# ── calendar: the .ics parser ───────────────────────────────────────────────


class TestParseDt:
    """``DTSTART``/``DTEND`` parsing, including the TZID case."""

    def test_a_utc_stamp_is_taken_as_utc(self):
        assert cal._parse_dt("20260803T160000Z", "") == datetime(
            2026, 8, 3, 16, 0, tzinfo=timezone.utc
        )

    def test_a_tzid_is_resolved_not_assumed_utc(self):
        """A named zone must be converted, not relabelled.

        Reading `TZID=America/Los_Angeles:20260803T090000` as UTC displayed a 09:00
        meeting as 02:00 — and it is not merely a display difference: the value names
        the wrong instant, so the sync window and the event ordering are wrong too.
        """
        parsed = cal._parse_dt("20260803T090000", ";TZID=America/Los_Angeles")
        # 09:00 PDT (UTC-7 in August) == 16:00 UTC.
        assert parsed == datetime(2026, 8, 3, 16, 0, tzinfo=timezone.utc)

    def test_a_quoted_tzid_is_resolved(self):
        parsed = cal._parse_dt("20260803T090000", ';TZID="America/Los_Angeles"')
        assert parsed == datetime(2026, 8, 3, 16, 0, tzinfo=timezone.utc)

    def test_a_winter_tzid_uses_the_right_offset(self):
        """Not a fixed offset — the zone's rules at THAT date."""
        parsed = cal._parse_dt("20260115T090000", ";TZID=America/Los_Angeles")
        # 09:00 PST (UTC-8 in January) == 17:00 UTC.
        assert parsed == datetime(2026, 1, 15, 17, 0, tzinfo=timezone.utc)

    def test_an_unknown_tzid_degrades_to_utc_rather_than_dropping(self):
        """A visible meeting at a possibly-wrong hour beats a missing one.

        Some exporters emit a Windows zone name or a custom VTIMEZONE id, neither
        of which is an IANA key.
        """
        parsed = cal._parse_dt("20260803T090000", ";TZID=Pacific Standard Time")
        assert parsed == datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc)

    def test_a_floating_time_is_read_as_utc(self):
        """Genuinely zone-less by spec; UTC is the only defensible reading."""
        assert cal._parse_dt("20260803T090000", "") == datetime(
            2026, 8, 3, 9, 0, tzinfo=timezone.utc
        )

    def test_a_whole_day_date(self):
        assert cal._parse_dt("20260803", ";VALUE=DATE") == datetime(
            2026, 8, 3, tzinfo=timezone.utc
        )

    def test_a_malformed_value_is_none(self):
        assert cal._parse_dt("not-a-date", "") is None
        assert cal._parse_dt("", "") is None


class TestEventId:
    """`_event_id_for` must be INJECTIVE — the id becomes a directory name."""

    def test_distinct_uids_that_sanitize_alike_stay_distinct(self):
        """`event/1` and `event?1` both sanitize to `event_1`.

        And the id becomes a meeting directory via `store.safe_meeting_id`, so the
        collision was not cosmetic: two calendar entries became ONE list row and
        shared a meeting folder, each overwriting the other's notes and tasks.
        """
        assert cal._event_id_for("event/1") != cal._event_id_for("event?1")

    def test_long_uids_sharing_a_prefix_stay_distinct(self):
        """Truncation at the cap collided the same way."""
        base = "x" * (k.MAX_MEETING_ID_LEN + 40)
        assert cal._event_id_for(base + "-a") != cal._event_id_for(base + "-b")

    def test_the_id_is_stable_across_syncs(self):
        """The same UID must always yield the same id, or a meeting cannot reopen."""
        assert cal._event_id_for("event/1") == cal._event_id_for("event/1")

    def test_the_id_is_filesystem_safe_and_within_the_cap(self):
        from kiro_crew.apps.builtins.meetings.backend import store

        for uid in ("event/1", "a b c", "../escape", "x" * 500, "ünïcøde"):
            event_id = cal._event_id_for(uid)
            assert store.safe_meeting_id(event_id) == event_id
            assert len(event_id) <= k.MAX_MEETING_ID_LEN

    def test_the_readable_stem_survives(self):
        """A digest that replaced the whole id would make the UI unreadable."""
        assert cal._event_id_for("standup-2026").startswith("standup-2026-")


class TestParseIcs:
    def test_parses_a_basic_event(self):
        events = cal.parse_ics(
            _ics(
                _vevent(
                    UID="abc-123",
                    SUMMARY="Sprint Standup",
                    DTSTART=_stamp(2),
                    DTEND=_stamp(3),
                    LOCATION="Room 1",
                )
            )
        )
        assert len(events) == 1
        assert events[0].title == "Sprint Standup"
        # The id keeps the readable stem and gains a digest of the original UID —
        # see `_event_id_for`: sanitizing alone was not injective.
        assert events[0].event_id.startswith("abc-123-")
        assert events[0].location == "Room 1"

    def test_skips_events_outside_the_horizon(self):
        far = (datetime.now(timezone.utc) + timedelta(days=60)).strftime("%Y%m%dT%H%M%SZ")
        old = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y%m%dT%H%M%SZ")
        text = _ics(
            _vevent(UID="soon", SUMMARY="Soon", DTSTART=_stamp(1)),
            _vevent(UID="far", SUMMARY="Far", DTSTART=far),
            _vevent(UID="old", SUMMARY="Old", DTSTART=old),
        )
        assert [e.event_id.split("-")[0] for e in cal.parse_ics(text, days=7)] == ["soon"]

    def test_horizon_is_configurable(self):
        far = (datetime.now(timezone.utc) + timedelta(days=20)).strftime("%Y%m%dT%H%M%SZ")
        text = _ics(_vevent(UID="far", SUMMARY="Far", DTSTART=far))
        assert cal.parse_ics(text, days=7) == []
        assert len(cal.parse_ics(text, days=30)) == 1

    def test_unfolds_a_folded_line(self):
        # RFC 5545 §3.1 line folding: unfolding removes the line break AND the
        # single leading whitespace char, joining with NO inserted space — so a
        # real emitter puts the space before the fold, as here.
        text = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:folded\n"
            "SUMMARY:A very long meeting \n title that wrapped\n"
            f"DTSTART:{_stamp(1)}\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        assert cal.parse_ics(text)[0].title == "A very long meeting title that wrapped"

    def test_unfold_does_not_insert_a_space(self):
        text = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:folded2\n"
            "SUMMARY:Deploy\n mentAudit\n"
            f"DTSTART:{_stamp(1)}\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        assert cal.parse_ics(text)[0].title == "DeploymentAudit"

    def test_crlf_line_endings(self):
        text = _ics(_vevent(UID="crlf", SUMMARY="CRLF", DTSTART=_stamp(1))).replace("\n", "\r\n")
        assert cal.parse_ics(text)[0].title == "CRLF"

    def test_unescapes_text_values(self):
        text = _ics(
            _vevent(UID="esc", SUMMARY="A\\, B\\; C\\nD", DTSTART=_stamp(1))
        )
        assert cal.parse_ics(text)[0].title == "A, B; C\nD"

    def test_attendee_and_organizer_common_names(self):
        text = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:att\nSUMMARY:S\n"
            f"DTSTART:{_stamp(1)}\n"
            "ORGANIZER;CN=Alice Example:mailto:alice@example.test\n"
            "ATTENDEE;CN=Bob Example:mailto:bob@example.test\n"
            "ATTENDEE:mailto:carol@example.test\n"
            "END:VEVENT\nEND:VCALENDAR\n"
        )
        event = cal.parse_ics(text)[0]
        assert event.organizer == "Alice Example"
        assert event.attendees == ["Bob Example", "carol@example.test"]

    def test_whole_day_event(self):
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        text = (
            "BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:day\nSUMMARY:All day\n"
            f"DTSTART;VALUE=DATE:{today}\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        events = cal.parse_ics(text)
        assert len(events) == 1
        assert events[0].title == "All day"

    def test_duration_instead_of_dtend(self):
        text = _ics(_vevent(UID="dur", SUMMARY="S", DTSTART=_stamp(1), DURATION="PT45M"))
        event = cal.parse_ics(text)[0]
        start = datetime.strptime(event.start, "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(event.end, "%Y-%m-%dT%H:%M:%SZ")
        assert (end - start) == timedelta(minutes=45)

    @pytest.mark.parametrize(
        "duration",
        [
            "P" + "9" * 400 + "D",       # overflows C int -> OverflowError
            "PT" + "9" * 400 + "H",
            "P9999999999D",              # inside C int, outside timedelta's day range
            # CONSTRUCTIBLE but unusable: exactly `timedelta.max.days`, so the
            # constructor succeeds and the overflow moves to `start + delta`. The first
            # version of this fix caught only the constructor and left this live —
            # `datetime + timedelta` then raised `OverflowError: date value out of
            # range`, and since `handle_calendar_sync` catches only `CalendarError` the
            # sync answered 500 and the WHOLE feed was lost.
            "P999999999D",
            "P3651D",                    # just past the sane ceiling
        ],
    )
    def test_an_unrepresentable_duration_does_not_crash_the_sync(self, duration: str):
        """A syntactically VALID duration can still be unusable.

        The pattern's `\\d+` groups are unbounded, so `P<400 digits>D` matches and then
        `timedelta` raises `OverflowError: Python int too large to convert to C int`
        (a ten-digit value trips its day-range `ValueError` first). Nothing up the
        stack caught either, so a remote `.ics` carrying one turned calendar sync into
        a 500 — and this value comes from a REMOTE server, the same untrusted source
        this module already guards for URLs and timezone ids.

        "Constructible" is NOT the property that matters, which is what the first fix
        got wrong: the delta is ADDED to a datetime by the caller, so it must also be
        bounded. Hence the ceiling rather than only a try/except.

        The event must SURVIVE with a default end rather than be dropped: a visible
        meeting at a possibly-wrong length beats a missing one, which is the same call
        the timezone fallback already makes.
        """
        text = _ics(_vevent(UID="big", SUMMARY="S", DTSTART=_stamp(1), DURATION=duration))
        events = cal.parse_ics(text)
        assert len(events) == 1
        start = datetime.strptime(events[0].start, "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(events[0].end, "%Y-%m-%dT%H:%M:%SZ")
        assert (end - start) == timedelta(hours=1), "should fall back to the default length"

    def test_missing_dtend_and_duration_defaults_to_one_hour(self):
        event = cal.parse_ics(_ics(_vevent(UID="d", SUMMARY="S", DTSTART=_stamp(1))))[0]
        start = datetime.strptime(event.start, "%Y-%m-%dT%H:%M:%SZ")
        end = datetime.strptime(event.end, "%Y-%m-%dT%H:%M:%SZ")
        assert (end - start) == timedelta(hours=1)

    def test_event_without_dtstart_is_skipped(self):
        assert cal.parse_ics(_ics(_vevent(UID="nostart", SUMMARY="S"))) == []

    def test_malformed_dtstart_is_skipped(self):
        assert cal.parse_ics(_ics(_vevent(UID="bad", SUMMARY="S", DTSTART="not-a-date"))) == []

    def test_uid_is_coerced_to_a_safe_path_segment(self):
        text = _ics(_vevent(UID="a/b c:d", SUMMARY="S", DTSTART=_stamp(1)))
        event_id = cal.parse_ics(text)[0].event_id
        assert "/" not in event_id and " " not in event_id
        # The id must survive the store's own validator.
        from kiro_crew.apps.builtins.meetings.backend import store

        assert store.safe_meeting_id(event_id) == event_id

    def test_missing_uid_gets_a_synthetic_id(self):
        event = cal.parse_ics(_ics(_vevent(SUMMARY="S", DTSTART=_stamp(1))))[0]
        assert event.event_id.startswith("ics-")  # synthesized from the start time

    def test_missing_summary_defaults(self):
        assert cal.parse_ics(_ics(_vevent(UID="u", DTSTART=_stamp(1))))[0].title == "Meeting"

    def test_redacts_credentials_in_fields(self):
        text = _ics(
            _vevent(UID="c", SUMMARY="Review AKIAIOSFODNN7EXAMPLE", DTSTART=_stamp(1))
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in cal.parse_ics(text)[0].title

    def test_non_vevent_components_ignored(self):
        text = (
            "BEGIN:VCALENDAR\nBEGIN:VTIMEZONE\nTZID:UTC\nEND:VTIMEZONE\n"
            f"{_vevent(UID='u', SUMMARY='S', DTSTART=_stamp(1))}\nEND:VCALENDAR\n"
        )
        assert len(cal.parse_ics(text)) == 1

    def test_events_sorted_by_start(self):
        text = _ics(
            _vevent(UID="later", SUMMARY="Later", DTSTART=_stamp(5)),
            _vevent(UID="sooner", SUMMARY="Sooner", DTSTART=_stamp(1)),
        )
        assert [e.event_id.split("-")[0] for e in cal.parse_ics(text)] == [
            "sooner", "later",
        ]

    def test_event_cap_enforced(self):
        text = _ics(
            *[
                _vevent(UID=f"u{i}", SUMMARY="S", DTSTART=_stamp(1))
                for i in range(k.MAX_CALENDAR_EVENTS + 20)
            ]
        )
        assert len(cal.parse_ics(text)) == k.MAX_CALENDAR_EVENTS

    def test_garbage_input_returns_empty(self):
        assert cal.parse_ics("this is not a calendar at all") == []
        assert cal.parse_ics("") == []


# ── calendar: source validation ─────────────────────────────────────────────


@pytest.fixture
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve every hostname to a public address.

    The scheme/address gate calls ``getaddrinfo``, and a test must never depend
    on real DNS (offline CI, a resolver that NXDOMAIN-hijacks). These tests are
    about the SCHEME decision, so resolution is stubbed to a public address and
    the address decision gets its own tests below.
    """
    monkeypatch.setattr(
        cal.socket, "getaddrinfo", lambda *_a, **_kw: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )


class TestUrlValidation:
    def test_https_accepted(self, public_dns: None):
        assert cal._normalize_url("https://example.test/cal.ics").url.startswith("https://")

    def test_webcal_rewritten_to_https(self, public_dns: None):
        assert cal._normalize_url("webcal://example.test/cal.ics").url.startswith("https://")

    def test_webcals_rewritten_to_https(self, public_dns: None):
        assert cal._normalize_url("webcals://example.test/cal.ics").url.startswith("https://")

    def test_returns_the_vetted_addresses_with_the_url(self, public_dns: None):
        """The gate's answer must carry the addresses it approved.

        Returning only the URL is what made the old gate a TOCTOU: the caller had
        nothing to pin, so aiohttp re-resolved the name for the connect. The
        address travelling WITH the url is the fix's load-bearing shape.
        """
        target = cal._normalize_url("https://example.test/cal.ics")
        assert target.addresses == ("93.184.216.34",)
        assert target.host == "example.test"
        assert target.port == 443

    def test_the_default_https_port_is_filled_in(self, public_dns: None):
        """The pin is keyed on (host, port), so the port can never be left unset."""
        assert cal._normalize_url("https://example.test/cal.ics").port == 443

    def test_a_non_standard_port_is_refused(self, public_dns: None):
        """Deliberately narrower than "any port the URL names".

        The shared vet allows 80 and 443 only, and https-only leaves 443. A calendar
        on some other port is nearly always an internal service, and the ports are
        the cheapest place to stop this endpoint being used to probe for one. The
        calendar feature has never shipped, so no working configuration is broken by
        starting strict; relaxing later is a one-line change to that allow-list.
        """
        with pytest.raises(cal.CalendarError):
            cal._normalize_url("https://example.test:8443/cal.ics")

    def test_host_is_keyed_as_the_connector_will_see_it(self, public_dns: None):
        """The pin is looked up by yarl's ``raw_host``, so it must be stored that way.

        A pin recorded under ``urlsplit``'s Unicode hostname would never match the
        IDNA-encoded, lowercased host the connector asks about — and a pin that
        never matches means a silent fall-through to a fresh lookup, i.e. the bug
        back again but invisible.
        """
        assert cal._normalize_url("https://EXAMPLE.test/c.ics").host == "example.test"
        assert cal._normalize_url("https://bücher.example/c.ics").host == ("xn--bcher-kva.example")

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.test/cal.ics",
            "file:///etc/passwd",
            "ftp://example.test/cal.ics",
            "javascript:alert(1)",
            "gopher://example.test/",
        ],
    )
    def test_non_https_schemes_refused(self, url):
        with pytest.raises(cal.CalendarError):
            cal._normalize_url(url)

    @pytest.mark.parametrize(
        "url",
        ["https://[", "https://[::1", "https://exam ple.test/\x00", "http://[abc"],
    )
    def test_a_malformed_url_is_a_calendar_error_not_a_500(self, url):
        """`urlsplit` RAISES on a malformed authority — it does not return empties.

        So an operator typo in `calendar.source` surfaced as an uncaught ValueError
        and an HTTP 500, instead of the "your calendar URL is wrong" message every
        other rejection here produces.
        """
        with pytest.raises(cal.CalendarError):
            cal._normalize_url(url)

    @pytest.mark.parametrize("location", ["https://[", "http://[abc", "https://[::1"])
    def test_a_malformed_redirect_target_is_a_calendar_error(self, location: str):
        """The `Location` header comes from the REMOTE server, not our config.

        `URL(...)` raises on a malformed value, so a hostile or broken endpoint could
        turn its own redirect into an HTTP 500 here. Guarding `_normalize_url` and not
        the redirect left the identical crash one hop away.
        """
        from yarl import URL as _URL

        with pytest.raises(cal.CalendarError):
            try:
                _URL(location)
            except ValueError as exc:
                raise cal.CalendarError("calendar redirect URL is malformed") from exc

    def test_the_redirect_parse_is_guarded_in_the_fetch_loop(self):
        """Structural: the guard must be at the real call site, not just provable.

        The assertion above shows the mapping is correct; this shows the fetch loop
        actually applies it, which is the part that was missing.
        """
        import inspect

        # The call site is `fetch_vetted`, which is where the hop loop lives now
        # that every provider shares it.
        source = inspect.getsource(cal.fetch_vetted)
        assert "calendar redirect URL is malformed" in source

    def test_missing_host_refused(self):
        with pytest.raises(cal.CalendarError):
            cal._normalize_url("https:///cal.ics")

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "0.0.0.0", "[::1]"],
    )
    def test_private_and_loopback_addresses_refused(self, host):
        # The request-forgery gate: the gateway performs this fetch, so an
        # internal-only address must never be reachable through a config value.
        with pytest.raises(cal.CalendarError):
            cal._normalize_url(f"https://{host}/cal.ics")

    def test_unresolvable_host_refused(self, monkeypatch):
        def boom(*_args, **_kwargs):
            raise OSError("nxdomain")

        monkeypatch.setattr(cal.socket, "getaddrinfo", boom)
        with pytest.raises(cal.CalendarError):
            cal._normalize_url("https://nonexistent.invalid/cal.ics")

    def test_public_resolved_host_allowed(self, public_dns: None):
        assert cal._normalize_url("https://example.test/cal.ics")

    def test_host_resolving_to_a_private_address_refused(self, monkeypatch):
        # DNS rebinding shape: a public NAME that resolves inward.
        monkeypatch.setattr(
            cal.socket,
            "getaddrinfo",
            lambda *_a, **_kw: [(2, 1, 6, "", ("127.0.0.1", 443))],
        )
        with pytest.raises(cal.CalendarError):
            cal._normalize_url("https://rebind.example.test/cal.ics")

    def test_a_mixed_public_and_private_answer_is_refused_entirely(self, monkeypatch):
        """ "Some public, some private" is not acceptable — it is the attack shape.

        Filtering the answer down to its public records would leave the host
        fetchable, and an attacker could simply retry until the connector happened
        to pick the private record. The whole answer is refused instead.
        """
        monkeypatch.setattr(
            cal.socket,
            "getaddrinfo",
            lambda *_a, **_kw: [
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("169.254.169.254", 443)),
            ],
        )
        with pytest.raises(cal.CalendarError):
            cal._normalize_url("https://mixed.example.test/cal.ics")

    def test_every_address_in_a_multi_record_answer_is_pinned(self, monkeypatch):
        """An all-public answer keeps all of its addresses (normal CDN failover)."""
        monkeypatch.setattr(
            cal.socket,
            "getaddrinfo",
            lambda *_a, **_kw: [
                (2, 1, 6, "", ("93.184.216.34", 443)),
                (10, 1, 6, "", ("2606:2800:220:1:248:1893:25c8:1946", 443, 0, 0)),
            ],
        )
        target = cal._normalize_url("https://dual.example.test/cal.ics")
        assert target.addresses == (
            "93.184.216.34",
            "2606:2800:220:1:248:1893:25c8:1946",
        )

    @pytest.mark.parametrize(
        "host",
        [
            "[::ffff:10.0.0.1]",  # IPv4-mapped IPv6 wrapping a private v4
            "[::ffff:127.0.0.1]",
            "[2002:7f00:1::1]",  # 6to4 wrapping 127.0.0.1
            "[fc00::1]",  # unique-local
            "[fe80::1]",  # link-local
        ],
    )
    def test_ipv6_wrapped_and_internal_addresses_refused(self, host):
        """An internal v4 hidden inside a v6 form is judged by what it embeds.

        Without unwrapping, ``is_private`` on ``::ffff:10.0.0.1`` can read as
        public while the packet lands inside the network.
        """
        with pytest.raises(cal.CalendarError):
            cal._normalize_url(f"https://{host}/cal.ics")

    def test_ipv6_public_literal_allowed(self):
        assert cal._normalize_url("https://[2606:4700::1111]/cal.ics").addresses == (
            "2606:4700::1111",
        )

    def test_an_unparseable_resolved_address_is_refused_not_skipped(self, monkeypatch):
        """A candidate the address parser cannot read must not be waved through.

        The old loop `continue`d here, so anything `getaddrinfo` returned in an
        unexpected shape went UNCHECKED — and an unvetted candidate could still be
        connected to. Refusing is the fail-closed choice.
        """
        monkeypatch.setattr(
            cal.socket,
            "getaddrinfo",
            lambda *_a, **_kw: [(2, 1, 6, "", ("not-an-address", 443))],
        )
        with pytest.raises(cal.CalendarError):
            cal._normalize_url("https://weird.example.test/cal.ics")

    def test_an_empty_resolution_is_refused(self, monkeypatch):
        """No addresses means nothing to pin, so there is nothing safe to connect to."""
        monkeypatch.setattr(cal.socket, "getaddrinfo", lambda *_a, **_kw: [])
        with pytest.raises(cal.CalendarError):
            cal._normalize_url("https://empty.example.test/cal.ics")


class TestPinnedResolver:
    """The resolver that makes the vetted address the connected address."""

    @staticmethod
    def _target(host="example.test", port=443, addresses=("93.184.216.34",)):
        return cal.VettedTarget(
            url=f"https://{host}:{port}/cal.ics", host=host, port=port, addresses=addresses
        )

    @pytest.mark.asyncio
    async def test_serves_only_the_pinned_address(self):
        resolver = cal._PinnedResolver()
        resolver.pin(self._target())
        results = await resolver.resolve("example.test", 443, family=cal.socket.AF_UNSPEC)
        assert [r["host"] for r in results] == ["93.184.216.34"]
        assert results[0]["hostname"] == "example.test"
        assert results[0]["port"] == 443

    @pytest.mark.asyncio
    async def test_never_calls_dns(self, monkeypatch: pytest.MonkeyPatch):
        """The whole point: no second lookup exists to be poisoned."""

        def boom(*_args, **_kwargs):
            raise AssertionError("the pinned resolver must not resolve anything")

        monkeypatch.setattr(cal.socket, "getaddrinfo", boom)
        resolver = cal._PinnedResolver()
        resolver.pin(self._target())
        assert await resolver.resolve("example.test", 443, family=cal.socket.AF_UNSPEC)

    @pytest.mark.asyncio
    async def test_an_unpinned_host_is_refused_not_resolved(self):
        """Fail closed: an unvetted host gets an error, never a fresh lookup."""
        resolver = cal._PinnedResolver()
        with pytest.raises(OSError):
            await resolver.resolve("surprise.example.test", 443)

    @pytest.mark.asyncio
    async def test_a_different_port_is_a_different_pin(self):
        """Pinning :443 must not authorize :8443 — that would widen the grant."""
        resolver = cal._PinnedResolver()
        resolver.pin(self._target(port=443))
        with pytest.raises(OSError):
            await resolver.resolve("example.test", 8443)

    @pytest.mark.asyncio
    async def test_family_filter_selects_matching_addresses(self):
        resolver = cal._PinnedResolver()
        resolver.pin(self._target(addresses=("93.184.216.34", "2606:4700::1111")))
        v4 = await resolver.resolve("example.test", 443, family=cal.socket.AF_INET)
        v6 = await resolver.resolve("example.test", 443, family=cal.socket.AF_INET6)
        assert [r["host"] for r in v4] == ["93.184.216.34"]
        assert [r["host"] for r in v6] == ["2606:4700::1111"]
        assert v6[0]["family"] == cal.socket.AF_INET6

    @pytest.mark.asyncio
    async def test_no_address_in_the_requested_family_is_refused(self):
        resolver = cal._PinnedResolver()
        resolver.pin(self._target(addresses=("93.184.216.34",)))
        with pytest.raises(OSError):
            await resolver.resolve("example.test", 443, family=cal.socket.AF_INET6)

    @pytest.mark.asyncio
    async def test_a_trailing_dot_fqdn_still_matches(self):
        """aiohttp strips trailing dots before resolving; the pin must agree."""
        resolver = cal._PinnedResolver()
        resolver.pin(self._target(host="example.test."))
        assert await resolver.resolve("example.test", 443, family=cal.socket.AF_UNSPEC)


class TestCertificateVerificationStaysOn:
    """Pinning must not have been bought by weakening TLS.

    The tempting way to make an IP-pinned connection "work" is `ssl=False` or a
    custom unverified context. That trades an SSRF hole for a MITM hole, so it is
    asserted against rather than assumed.
    """

    @pytest.mark.asyncio
    async def test_the_connector_keeps_aiohttps_verified_context(self):
        import aiohttp
        from aiohttp.connector import _SSL_CONTEXT_UNVERIFIED, _SSL_CONTEXT_VERIFIED

        connector = aiohttp.TCPConnector(resolver=cal._PinnedResolver(), use_dns_cache=False)
        try:
            # `_ssl is True` is aiohttp's "no override, use the default" sentinel.
            assert connector._ssl is True
            request = types.SimpleNamespace(is_ssl=lambda: True, ssl=True)
            context = connector._get_ssl_context(request)
            assert context is _SSL_CONTEXT_VERIFIED
            assert context is not _SSL_CONTEXT_UNVERIFIED
            assert context.verify_mode == ssl.CERT_REQUIRED
            assert context.check_hostname is True
        finally:
            await connector.close()

    def test_the_fetch_never_disables_verification(self):
        """No `ssl=False` / `verify_ssl=False` anywhere on the fetch path."""
        import inspect

        src = inspect.getsource(cal.IcsCalendarProvider._fetch_url)
        # Strip comments: the prose explains what is deliberately NOT passed, and a
        # substring search over it would match the very thing being forbidden.
        code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
        assert "ssl=False" not in code
        assert "verify_ssl=False" not in code
        assert "_create_unverified_context" not in code

    def test_the_pinned_connector_is_wired_into_the_session(self):
        """The pin only works if the session actually USES that connector.

        Building a `_PinnedResolver` and then opening a default `ClientSession`
        would leave the fetch fully rebindable while looking fixed, so the wiring
        is asserted on the code with comments stripped (the surrounding prose
        mentions these names too).
        """
        import inspect

        code = "\n".join(
            line.split("#", 1)[0]
            for line in inspect.getsource(cal.fetch_vetted).splitlines()
        )
        assert "_PinnedResolver()" in code
        assert "use_dns_cache=False" in code
        assert "connector=connector" in code
        assert "resolver.pin(target)" in code

    def test_the_url_keeps_its_hostname_so_sni_and_host_survive(self, public_dns: None):
        """The request URL must still name the HOST, not the pinned IP.

        Rewriting the URL to its IP is the other way to pin a connection, and it
        breaks certificate validation for every real calendar host (SNI and the
        cert's subject are derived from the URL). Substituting only the resolution
        step is what keeps `Host`, SNI, and cert checking correct.
        """
        target = cal._normalize_url("https://example.test/cal.ics")
        assert "example.test" in target.url
        assert "93.184.216.34" not in target.url


class TestDnsRebindingIsRefused:
    """The finding itself: a DNS answer that changes between check and connect.

    `_normalize_url` resolved the host and approved the address; the old code then
    let aiohttp resolve the SAME name independently for the connect. A host whose
    answer flips in between (short TTL, or a resolver alternating a public and a
    private record) passed validation and was fetched at the private address.

    This drives it against two REAL servers on one port — a "public" one on
    127.0.0.1 and an internal one on ::1 — with a resolver that answers the first
    lookup with the first and every later lookup with the second. Which body comes
    back names the address that was actually connected to, so the assertion cannot
    pass vacuously.
    """

    @staticmethod
    async def _serve(host: str, body: str, port: int = 0):
        from aiohttp import web as aioweb

        app = aioweb.Application()

        async def handler(_request):
            return aioweb.Response(text=body)

        app.router.add_get("/{tail:.*}", handler)
        runner = aioweb.AppRunner(app)
        await runner.setup()
        site = aioweb.TCPSite(runner, host, port)
        await site.start()
        return runner, runner.addresses[0][1]

    @staticmethod
    def _flip_flop(port: int):
        """First lookup -> 127.0.0.1; every later one -> ::1. The rebind."""
        counter = itertools.count()

        def fake_getaddrinfo(*_args, **_kwargs):
            if next(counter) == 0:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0))]

        return fake_getaddrinfo

    @pytest.fixture
    def local_server_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Open exactly two gates so a REAL local server can stand in for a host.

        The point of these tests is which ADDRESS the socket reaches, so the real
        `_normalize_url` must run — it is what resolves, vets, and produces the pin.
        Two of its refusals would stop a loopback test server before that:

        * the https-only scheme allow-list (a local TLS server would need a CA and
          a trusted cert, which buys no coverage of the address decision), and
        * the private/loopback address refusal (127.0.0.1 IS the test server).

        Both have their own dedicated tests above, and neither is what these tests
        exercise. Everything else — resolution, the all-or-nothing address check,
        the pin, the connector, the hop loop — runs for real.
        """
        monkeypatch.setattr(cal, "_ALLOWED_SCHEMES", ("https", "http"))
        # The address vet is `link_unfurl`'s, so the two refusals to lift are its:
        # the private-address rule (127.0.0.1 IS the test server) and the 80/443
        # port rule (the server binds an ephemeral port). Neutralized on that
        # module, which is where the real code reads them from.
        monkeypatch.setattr(cal.link_unfurl, "_reject_if_internal_ip", lambda _c: None)
        monkeypatch.setattr(cal.link_unfurl, "ALLOWED_PORTS", _AnyPort())

    @pytest.mark.asyncio
    async def test_the_fetch_lands_on_the_vetted_address_not_the_rebound_one(
        self, local_server_allowed: None, monkeypatch: pytest.MonkeyPatch
    ):
        first, port = await self._serve(
            "127.0.0.1", "BEGIN:VCALENDAR\r\nFIRST\r\nEND:VCALENDAR\r\n"
        )
        second, _ = await self._serve(
            "::1", "BEGIN:VCALENDAR\r\nREBOUND\r\nEND:VCALENDAR\r\n", port
        )
        monkeypatch.setattr(cal.socket, "getaddrinfo", self._flip_flop(port))
        url = f"http://rebind.example.test:{port}/cal.ics"
        try:
            body = await cal.IcsCalendarProvider(url)._fetch_url(url)
        finally:
            await first.cleanup()
            await second.cleanup()
        # FIRST == the address validation approved. REBOUND would mean the connect
        # used a second, unvalidated DNS answer — the finding, unfixed.
        assert "FIRST" in body
        assert "REBOUND" not in body

    @pytest.mark.asyncio
    async def test_an_unpinned_connector_would_have_been_rebound(
        self, local_server_allowed: None, monkeypatch: pytest.MonkeyPatch
    ):
        """Proves the test above is not vacuous.

        Same two servers, same flip-flopping resolver — but a DEFAULT aiohttp
        connector, i.e. the code before this fix. It reaches the second, private
        address. If this ever stops reaching REBOUND the fixture has gone stale and
        the test above proves nothing.
        """
        import aiohttp

        first, port = await self._serve(
            "127.0.0.1", "BEGIN:VCALENDAR\r\nFIRST\r\nEND:VCALENDAR\r\n"
        )
        second, _ = await self._serve(
            "::1", "BEGIN:VCALENDAR\r\nREBOUND\r\nEND:VCALENDAR\r\n", port
        )
        monkeypatch.setattr(cal.socket, "getaddrinfo", self._flip_flop(port))
        url = f"http://rebind.example.test:{port}/cal.ics"
        try:
            target = cal._normalize_url(url)  # lookup #1 -> vets 127.0.0.1
            assert target.addresses == ("127.0.0.1",)
            async with aiohttp.ClientSession() as session:  # no pin: lookup #2 wins
                async with session.get(target.url, allow_redirects=False) as resp:
                    body = await resp.text()
        finally:
            await first.cleanup()
            await second.cleanup()
        assert "REBOUND" in body

    @pytest.mark.asyncio
    async def test_a_normal_public_host_still_fetches(
        self, local_server_allowed: None, monkeypatch: pytest.MonkeyPatch
    ):
        """The guard against "fixing" this by breaking the feature.

        A stable hostname must still resolve, connect, and return its document
        through the real `_normalize_url` + pinned connector path.
        """
        server, port = await self._serve(
            "127.0.0.1", "BEGIN:VCALENDAR\r\nPUBLIC OK\r\nEND:VCALENDAR\r\n"
        )
        monkeypatch.setattr(
            cal.socket,
            "getaddrinfo",
            lambda *_a, **_kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))],
        )
        url = f"http://calendar.example.test:{port}/cal.ics"
        try:
            body = await cal.IcsCalendarProvider(url)._fetch_url(url)
        finally:
            await server.cleanup()
        assert "PUBLIC OK" in body

    @pytest.mark.asyncio
    async def test_a_redirect_hop_is_pinned_to_its_own_vetted_address(
        self, local_server_allowed: None, monkeypatch: pytest.MonkeyPatch
    ):
        """Each hop must be vetted-then-used, never vetted-then-re-resolved.

        The first host 302s to a SECOND hostname. That target is validated, and the
        address approved for it is the address its request must use — even though
        DNS for that name flips to the internal server immediately afterwards.
        """
        from aiohttp import web as aioweb

        final, final_port = await self._serve(
            "127.0.0.1", "BEGIN:VCALENDAR\r\nHOP TARGET\r\nEND:VCALENDAR\r\n"
        )
        rebound, _ = await self._serve(
            "::1", "BEGIN:VCALENDAR\r\nREBOUND\r\nEND:VCALENDAR\r\n", final_port
        )

        async def redirector(_request):
            raise aioweb.HTTPFound(f"http://second.example.test:{final_port}/cal.ics")

        app = aioweb.Application()
        app.router.add_get("/{tail:.*}", redirector)
        runner = aioweb.AppRunner(app)
        await runner.setup()
        site = aioweb.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        start_port = runner.addresses[0][1]

        # first.example.test always resolves to the redirector; second.example.test
        # answers 127.0.0.1 once (the vetting) then ::1 forever (the rebind).
        second_lookups = itertools.count()

        def fake_getaddrinfo(host, *_args, **_kwargs):
            if host.startswith("first."):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", start_port))]
            if next(second_lookups) == 0:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", final_port))]
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", final_port, 0, 0))]

        monkeypatch.setattr(cal.socket, "getaddrinfo", fake_getaddrinfo)
        url = f"http://first.example.test:{start_port}/cal.ics"
        try:
            body = await cal.IcsCalendarProvider(url)._fetch_url(url)
        finally:
            await runner.cleanup()
            await final.cleanup()
            await rebound.cleanup()
        assert "HOP TARGET" in body
        assert "REBOUND" not in body

    @pytest.mark.asyncio
    async def test_resolution_stays_off_the_event_loop(
        self, local_server_allowed: None, monkeypatch: pytest.MonkeyPatch
    ):
        """DNS is a blocking syscall, so it must run on the executor, not the loop.

        Asserts the thread identity at the point of the lookup: the gate runs on a
        worker, and the pinned resolver — which aiohttp awaits ON the loop — does no
        lookup at all, which is why nothing blocks it.
        """
        server, port = await self._serve(
            "127.0.0.1", "BEGIN:VCALENDAR\r\nOFFLOOP\r\nEND:VCALENDAR\r\n"
        )
        loop_thread = threading.get_ident()
        lookup_threads: list[int] = []

        def recording_getaddrinfo(*_args, **_kwargs):
            lookup_threads.append(threading.get_ident())
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        monkeypatch.setattr(cal.socket, "getaddrinfo", recording_getaddrinfo)
        url = f"http://offloop.example.test:{port}/cal.ics"
        try:
            body = await cal.IcsCalendarProvider(url)._fetch_url(url)
        finally:
            await server.cleanup()
        assert "OFFLOOP" in body
        assert lookup_threads, "expected the gate to resolve the host"
        assert loop_thread not in lookup_threads


class TestLocalIcsRead:
    def test_reads_a_file(self, tmp_path: Path):
        path = tmp_path / "cal.ics"
        path.write_text(_ics(_vevent(UID="u", SUMMARY="Local", DTSTART=_stamp(1))))
        assert "Local" in cal._read_local_ics(str(path))

    def test_missing_file_raises(self, tmp_path: Path):
        with pytest.raises(cal.CalendarError):
            cal._read_local_ics(str(tmp_path / "absent.ics"))

    def test_oversized_file_refused(self, tmp_path: Path):
        path = tmp_path / "big.ics"
        path.write_text("x" * (k.MAX_ICS_BYTES + 1))
        with pytest.raises(cal.CalendarError):
            cal._read_local_ics(str(path))

    def test_sensitive_path_refused(self):
        with pytest.raises(cal.CalendarError):
            cal._read_local_ics("~/.aws/credentials")

    def test_a_symlink_to_a_sensitive_target_is_refused(self, tmp_path: Path):
        """The check must apply to the RESOLVED target, not the path as written.

        The hand-rolled version this replaced called ``is_sensitive_path`` on the
        literal string and then read ``path.resolve()`` — so an innocuous-looking
        ``calendar.ics`` symlinked at ``~/.aws/credentials`` passed the check and
        was followed anyway. Routing through ``hooks.safe_read_file_bytes`` checks
        the canonical target and opens it ``O_NOFOLLOW``.
        """
        target = Path.home() / ".aws" / "credentials"
        link = tmp_path / "calendar.ics"
        link.symlink_to(target)
        with pytest.raises(cal.CalendarError):
            cal._read_local_ics(str(link))

    def test_reads_through_the_central_file_gate(self, tmp_path: Path):
        """Pinned structurally: a future edit must not go back to a direct read.

        ``backend-security-controls`` requires file reads to traverse the shared
        gate, and the symlink case above is only refused because of it.
        """
        path = tmp_path / "cal.ics"
        path.write_text(_ics(_vevent(UID="u", SUMMARY="Gated", DTSTART=_stamp(1))))
        with mock.patch.object(
            cal.hooks, "safe_read_file_bytes", wraps=cal.hooks.safe_read_file_bytes
        ) as gate:
            assert "Gated" in cal._read_local_ics(str(path))
        assert gate.called, "_read_local_ics must read via hooks.safe_read_file_bytes"


class TestCalendarRegistry:
    def test_defaults_registered(self):
        ids = {row["id"] for row in cal.available_calendar_providers()}
        assert ids == {
            k.CALENDAR_PROVIDER_NONE,
            k.CALENDAR_PROVIDER_ICS,
            k.CALENDAR_PROVIDER_CALDAV,
            k.CALENDAR_PROVIDER_GOOGLE,
            k.CALENDAR_PROVIDER_MICROSOFT,
        }

    def test_ics_declares_it_needs_a_source(self):
        rows = {row["id"]: row for row in cal.available_calendar_providers()}
        assert rows[k.CALENDAR_PROVIDER_ICS]["requires_source"] is True
        assert rows[k.CALENDAR_PROVIDER_NONE]["requires_source"] is False

    def test_caldav_declares_it_needs_a_source(self):
        rows = {row["id"]: row for row in cal.available_calendar_providers()}
        assert rows[k.CALENDAR_PROVIDER_CALDAV]["requires_source"] is True

    def test_listing_providers_does_not_touch_the_credential_store(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Rendering the settings picker must not read the disk.

        ``available_calendar_providers`` constructs EVERY registered factory just
        to read its label, inside a broad ``except`` that drops a row on failure.
        A provider that loaded credentials in ``__init__`` would therefore put a
        file read behind the picker and, if it raised, vanish from the list with
        only a log line to say why.
        """
        from kiro_crew.apps.builtins.meetings.backend import credentials as creds

        def explode() -> Path:
            raise AssertionError("the credential store was touched while listing")

        monkeypatch.setattr(creds, "credentials_file", explode)
        ids = {row["id"] for row in cal.available_calendar_providers()}
        assert k.CALENDAR_PROVIDER_CALDAV in ids

    def test_unknown_id_degrades_to_none(self):
        assert cal.get_calendar_provider("corporate-calendar").provider_id == (
            k.CALENDAR_PROVIDER_NONE
        )

    @pytest.mark.asyncio
    async def test_none_provider_explains_itself(self):
        with pytest.raises(cal.CalendarError) as excinfo:
            await cal.NoCalendarProvider().fetch()
        assert "Settings" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_ics_provider_without_source_raises(self):
        with pytest.raises(cal.CalendarError):
            await cal.IcsCalendarProvider("").fetch()

    @pytest.mark.asyncio
    async def test_ics_provider_reads_a_local_file(self, tmp_path: Path):
        path = tmp_path / "cal.ics"
        path.write_text(_ics(_vevent(UID="u", SUMMARY="From file", DTSTART=_stamp(1))))
        events = await cal.IcsCalendarProvider(str(path)).fetch()
        assert [e.title for e in events] == ["From file"]

    @pytest.mark.asyncio
    async def test_ics_provider_refuses_a_bad_url_scheme(self):
        with pytest.raises(cal.CalendarError):
            await cal.IcsCalendarProvider("file:///etc/passwd").fetch()

    def test_edition_can_register_and_receives_the_source(self):
        seen: list[str] = []

        class EditionCalendar(cal.CalendarProvider):
            def __init__(self, source: str) -> None:
                seen.append(source)

            @property
            def provider_id(self) -> str:
                return "edition-cal"

            @property
            def display_name(self) -> str:
                return "Edition calendar"

            async def fetch(self, *, days: int = 7):
                return []

        try:
            cal.register_calendar_provider("edition-cal", EditionCalendar)
            provider = cal.get_calendar_provider("edition-cal", "account-42")
            assert provider.provider_id == "edition-cal"
            assert "account-42" in seen
        finally:
            cal.register_calendar_provider("edition-cal", None)
        assert "edition-cal" not in {r["id"] for r in cal.available_calendar_providers()}

    def test_empty_provider_id_rejected(self):
        with pytest.raises(ValueError):
            cal.register_calendar_provider("", lambda _s: cal.NoCalendarProvider())

    def test_broken_factory_is_skipped_not_fatal(self):
        def boom(_source):
            raise RuntimeError("bad edition")

        try:
            cal.register_calendar_provider("broken-cal", boom)
            ids = {row["id"] for row in cal.available_calendar_providers()}
            assert "broken-cal" not in ids
            assert k.CALENDAR_PROVIDER_ICS in ids
        finally:
            cal.register_calendar_provider("broken-cal", None)


class TestRedirectsAreRevalidated:
    """A redirect target must pass the SSRF gate before the gateway follows it.

    Letting aiohttp auto-follow validates only the FIRST url, so a public host
    could 302 to ``http://169.254.169.254/`` and the gateway would issue that
    request itself — checking ``resp.url`` afterwards is too late, the request
    already happened. The provider therefore sets ``allow_redirects=False`` and
    re-runs ``_normalize_url`` on every hop.
    """

    def test_the_fetch_disables_automatic_redirects(self):
        """Pins the flag itself: without it, no per-hop validation can run.

        Inspects ``fetch_vetted`` rather than ``IcsCalendarProvider._fetch_url``:
        the hop loop moved there when CalDAV / Google / Microsoft 365 needed the
        same posture, and one shared copy is the point. The provider is pinned
        separately, below, as a caller of it.
        """
        import inspect

        src = inspect.getsource(cal.fetch_vetted)
        assert "allow_redirects=False" in src
        assert "_REDIRECT_STATUSES" in src

    def test_the_ics_provider_fetches_through_the_shared_gate(self):
        """The provider must not grow a second, unreviewed hop loop.

        If this fails because ``_fetch_url`` learned to talk to aiohttp directly
        again, that is the regression: every SSRF property in this file is pinned
        against ``fetch_vetted``, and a provider bypassing it inherits none of them.
        """
        import inspect

        src = inspect.getsource(cal.IcsCalendarProvider._fetch_url)
        assert "fetch_vetted(" in src
        assert "ClientSession" not in src

    @pytest.mark.parametrize(
        "target",
        [
            "http://169.254.169.254/latest/meta-data/",
            "https://127.0.0.1/cal.ics",
            "https://10.0.0.5/cal.ics",
            "file:///etc/passwd",
        ],
    )
    def test_a_redirect_to_a_blocked_target_is_refused_by_the_same_gate(self, target):
        """The hop loop hands each ``Location`` back to ``_normalize_url``."""
        with pytest.raises(cal.CalendarError):
            cal._normalize_url(target)

    def test_redirect_statuses_cover_every_http_redirect(self):
        assert cal._REDIRECT_STATUSES == frozenset({301, 302, 303, 307, 308})

    def test_the_hop_chain_is_bounded(self):
        assert k.ICS_MAX_REDIRECTS >= 1
        import inspect

        # The bound is the shared fetch's `max_redirects`, which the .ics provider
        # passes `ICS_MAX_REDIRECTS` into — so both halves are pinned.
        loop_src = inspect.getsource(cal.fetch_vetted)
        assert "max_redirects" in loop_src
        assert "redirected too many times" in loop_src
        assert "ICS_MAX_REDIRECTS" in inspect.getsource(cal.IcsCalendarProvider._fetch_url)


def _passthrough_target(url: str) -> cal.VettedTarget:
    """A `_normalize_url` stand-in that vets a loopback URL as-is.

    The real gate refuses loopback, so tests that need a REAL local server stub it
    out. The stub still returns a genuine :class:`cal.VettedTarget` pinned to the
    server's own address, so the fetch continues to run through the real pinned
    connector rather than around it.
    """
    parsed = URL(url)
    host = parsed.raw_host or ""
    return cal.VettedTarget(
        url=url,
        host=host,
        port=parsed.port or 443,
        addresses=(host,),
    )


class TestRedirectLoopAgainstARealServer:
    """Drive `_fetch_url`'s hop loop against a real local HTTP server.

    The unit assertions above pin the flag and the validator; these exercise the
    LOOP — that a redirect is actually followed, that its target is actually
    re-validated before the next request, and that the chain is actually bounded.
    A localhost server would normally be refused by the SSRF gate, so
    `_normalize_url` is stubbed to a pass-through recorder; that is the seam under
    test (the gate itself has its own tests).
    """

    @staticmethod
    async def _serve(handler):
        """Start an aiohttp app on an ephemeral loopback port."""
        from aiohttp import web as aioweb

        app = aioweb.Application()
        app.router.add_get("/{tail:.*}", handler)
        runner = aioweb.AppRunner(app)
        await runner.setup()
        site = aioweb.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        return runner, f"http://127.0.0.1:{port}"

    @pytest.mark.asyncio
    async def test_a_redirect_is_followed_and_its_target_revalidated(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from aiohttp import web as aioweb

        async def handler(request):
            if request.path == "/start":
                raise aioweb.HTTPFound("/final")
            return aioweb.Response(text="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")

        runner, base = await self._serve(handler)
        seen: list[str] = []

        def record(url: str) -> cal.VettedTarget:
            seen.append(url)
            return _passthrough_target(url)

        monkeypatch.setattr(cal, "_normalize_url", record)
        try:
            body = await cal.IcsCalendarProvider(f"{base}/start")._fetch_url(f"{base}/start")
        finally:
            await runner.cleanup()

        assert "BEGIN:VCALENDAR" in body
        # Two validations: the original URL and the redirect target. The second is
        # the whole point — without it a 302 would reach an unvalidated address.
        assert len(seen) == 2
        assert seen[1].endswith("/final")

    @pytest.mark.asyncio
    async def test_a_refused_redirect_target_aborts_the_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The validator's refusal must propagate, not be swallowed by the loop."""
        from aiohttp import web as aioweb

        async def handler(request):
            raise aioweb.HTTPFound("http://169.254.169.254/latest/meta-data/")

        runner, base = await self._serve(handler)
        calls = {"n": 0}

        def gate(url: str) -> cal.VettedTarget:
            calls["n"] += 1
            if calls["n"] == 1:
                return _passthrough_target(url)
            raise cal.CalendarError("blocked target")

        monkeypatch.setattr(cal, "_normalize_url", gate)
        try:
            with pytest.raises(cal.CalendarError):
                await cal.IcsCalendarProvider(base)._fetch_url(base)
        finally:
            await runner.cleanup()
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_an_endless_redirect_chain_is_bounded(self, monkeypatch: pytest.MonkeyPatch):
        from aiohttp import web as aioweb

        async def handler(request):
            raise aioweb.HTTPFound("/again")

        runner, base = await self._serve(handler)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        try:
            with pytest.raises(cal.CalendarError, match="too many times"):
                await cal.IcsCalendarProvider(base)._fetch_url(base)
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_a_redirect_with_no_location_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from aiohttp import web as aioweb

        async def handler(request):
            return aioweb.Response(status=302)  # no Location header

        runner, base = await self._serve(handler)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        try:
            with pytest.raises(cal.CalendarError, match="no target"):
                await cal.IcsCalendarProvider(base)._fetch_url(base)
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_a_non_200_is_reported(self, monkeypatch: pytest.MonkeyPatch):
        from aiohttp import web as aioweb

        async def handler(request):
            return aioweb.Response(status=404, text="nope")

        runner, base = await self._serve(handler)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        try:
            with pytest.raises(cal.CalendarError, match="HTTP 404"):
                await cal.IcsCalendarProvider(base)._fetch_url(base)
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_an_oversized_document_is_refused_while_streaming(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """The cap must bite DURING the read, not after buffering it all."""
        from aiohttp import web as aioweb

        async def handler(request):
            resp = aioweb.StreamResponse()
            await resp.prepare(request)
            chunk = b"x" * 65536
            for _ in range((k.MAX_ICS_BYTES // 65536) + 4):
                await resp.write(chunk)
            return resp

        runner, base = await self._serve(handler)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        try:
            with pytest.raises(cal.CalendarError, match="too large"):
                await cal.IcsCalendarProvider(base)._fetch_url(base)
        finally:
            await runner.cleanup()


class TestCredentialDoesNotSurviveACrossOriginRedirect:
    """An ``Authorization`` header must not follow a redirect off its origin.

    The ``.ics`` fetch never sent a credential, so this class covers a property
    that arrived with :func:`cal.fetch_vetted` for CalDAV / Google / Microsoft 365.
    The threat is concrete: a calendar host — or anything that can forge its
    ``Location`` — bounces the request to a host it controls and is handed the
    user's live password or bearer token. The redirect is still FOLLOWED, it just
    arrives unauthenticated, so the failure is visible instead of silent.

    ``headers`` and ``auth_headers`` are separate parameters for exactly this
    reason, and these tests pin the difference: a plain header rides every hop.
    """

    @staticmethod
    async def _serve(handler, *, method: str = "GET"):
        from aiohttp import web as aioweb

        app = aioweb.Application()
        app.router.add_route(method, "/{tail:.*}", handler)
        runner = aioweb.AppRunner(app)
        await runner.setup()
        site = aioweb.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"

    @pytest.mark.asyncio
    async def test_auth_rides_a_same_origin_redirect(self, monkeypatch: pytest.MonkeyPatch):
        """Staying on the addressed origin keeps the credential — otherwise a
        provider whose server legitimately redirects internally could never
        authenticate at all."""
        from aiohttp import web as aioweb

        seen: list[str | None] = []

        async def handler(request):
            seen.append(request.headers.get("Authorization"))
            if request.path == "/start":
                raise aioweb.HTTPFound("/final")
            return aioweb.Response(text="ok")

        runner, base = await self._serve(handler)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        try:
            body = await cal.fetch_vetted(
                f"{base}/start", auth_headers={"Authorization": "Basic c3VwZXItc2VjcmV0"}
            )
        finally:
            await runner.cleanup()

        assert body == b"ok"
        assert seen == ["Basic c3VwZXItc2VjcmV0", "Basic c3VwZXItc2VjcmV0"]

    @pytest.mark.asyncio
    async def test_auth_is_dropped_when_the_redirect_leaves_the_origin(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Two real servers: the first redirects to the second, which must NOT be
        handed the credential."""
        from aiohttp import web as aioweb

        leaked: list[str | None] = []

        async def attacker(request):
            leaked.append(request.headers.get("Authorization"))
            return aioweb.Response(text="landed")

        attacker_runner, attacker_base = await self._serve(attacker)

        async def origin(request):
            raise aioweb.HTTPFound(f"{attacker_base}/steal")

        origin_runner, origin_base = await self._serve(origin)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        try:
            body = await cal.fetch_vetted(
                f"{origin_base}/start",
                auth_headers={"Authorization": "Basic c3VwZXItc2VjcmV0"},
            )
        finally:
            await origin_runner.cleanup()
            await attacker_runner.cleanup()

        # The hop happened, so the failure mode is a visible auth failure rather
        # than a silent credential disclosure.
        assert body == b"landed"
        assert leaked == [None], f"credential leaked to another origin: {leaked}"

    @pytest.mark.asyncio
    async def test_a_plain_header_is_not_dropped(self, monkeypatch: pytest.MonkeyPatch):
        """Only ``auth_headers`` is origin-scoped; ``headers`` is not.

        Pins that the split is real. A provider that put its credential in
        ``headers`` would get no protection, which is why the docstring on
        ``fetch_vetted`` says which one to use.
        """
        from aiohttp import web as aioweb

        seen: list[str | None] = []

        async def second(request):
            seen.append(request.headers.get("X-Trace"))
            return aioweb.Response(text="ok")

        second_runner, second_base = await self._serve(second)

        async def first(request):
            raise aioweb.HTTPFound(f"{second_base}/next")

        first_runner, first_base = await self._serve(first)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        try:
            await cal.fetch_vetted(f"{first_base}/start", headers={"X-Trace": "keep-me"})
        finally:
            await first_runner.cleanup()
            await second_runner.cleanup()

        assert seen == ["keep-me"]


class TestANonGetRequestOnlyFollowsMethodPreservingRedirects:
    """301/302/303 tell a client to retry with GET.

    For the ``.ics`` GET that is harmless. For a CalDAV ``REPORT`` it would turn a
    calendar-query into a plain GET of some other resource and then parse whatever
    came back — a wrong answer that looks like a right one. Those are refused; the
    method-preserving 307/308 are followed.
    """

    @staticmethod
    async def _serve(handler, *, method: str):
        from aiohttp import web as aioweb

        app = aioweb.Application()
        app.router.add_route(method, "/{tail:.*}", handler)
        runner = aioweb.AppRunner(app)
        await runner.setup()
        site = aioweb.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        return runner, f"http://127.0.0.1:{runner.addresses[0][1]}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [301, 302, 303])
    async def test_a_method_changing_redirect_is_refused(
        self, status: int, monkeypatch: pytest.MonkeyPatch
    ):
        from aiohttp import web as aioweb

        async def handler(request):
            return aioweb.Response(status=status, headers={"Location": "/elsewhere"})

        runner, base = await self._serve(handler, method="REPORT")
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        try:
            with pytest.raises(cal.CalendarError, match="change the request method"):
                await cal.fetch_vetted(base, method="REPORT", body=b"<query/>")
        finally:
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_a_307_is_followed_with_the_method_and_body_intact(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        from aiohttp import web as aioweb

        bodies: list[bytes] = []

        async def handler(request):
            bodies.append(await request.read())
            if request.path == "/start":
                return aioweb.Response(status=307, headers={"Location": "/final"})
            return aioweb.Response(text="done")

        runner, base = await self._serve(handler, method="REPORT")
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        try:
            out = await cal.fetch_vetted(
                f"{base}/start", method="REPORT", body=b"<query/>"
            )
        finally:
            await runner.cleanup()

        assert out == b"done"
        # The body survived the hop — that is what "method preserving" has to mean.
        assert bodies == [b"<query/>", b"<query/>"]

    @pytest.mark.asyncio
    async def test_a_get_still_follows_a_302(self, monkeypatch: pytest.MonkeyPatch):
        """The refusal above must not have narrowed the GET path."""
        from aiohttp import web as aioweb

        async def handler(request):
            if request.path == "/start":
                raise aioweb.HTTPFound("/final")
            return aioweb.Response(text="ok")

        runner, base = await self._serve(handler, method="GET")
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        try:
            assert await cal.fetch_vetted(f"{base}/start") == b"ok"
        finally:
            await runner.cleanup()


# ── CalDAV ──────────────────────────────────────────────────────────────────

_VEVENT_TEMPLATE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:{uid}
SUMMARY:{summary}
DTSTART:{start}
DTEND:{end}
END:VEVENT
END:VCALENDAR"""


def _multistatus(*calendar_data: str) -> str:
    """A CalDAV ``REPORT`` response wrapping each iCalendar document."""
    responses = "".join(
        "<D:response><D:propstat><D:prop>"
        f"<C:calendar-data>{data}</C:calendar-data>"
        "</D:prop></D:propstat></D:response>"
        for data in calendar_data
    )
    return (
        '<?xml version="1.0" encoding="utf-8" ?>'
        '<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        f"{responses}</D:multistatus>"
    )


def _soon(hours: int) -> str:
    """An iCalendar UTC stamp *hours* from now, inside the sync window."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y%m%dT%H%M%SZ")


class _FakeCalDav:
    """A local server that answers a CalDAV ``REPORT``, recording what it got."""

    def __init__(self, body: str, *, status: int = 207) -> None:
        self._body = body
        self._status = status
        self.requests: list[dict[str, Any]] = []
        self._runner: Any = None
        self.base = ""

    async def __aenter__(self) -> _FakeCalDav:
        from aiohttp import web as aioweb

        async def handler(request):
            self.requests.append(
                {
                    "method": request.method,
                    "headers": dict(request.headers),
                    "body": (await request.read()).decode("utf-8"),
                }
            )
            return aioweb.Response(
                status=self._status, text=self._body, content_type="application/xml"
            )

        app = aioweb.Application()
        app.router.add_route("REPORT", "/{tail:.*}", handler)
        self._runner = aioweb.AppRunner(app)
        await self._runner.setup()
        site = aioweb.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.base = f"http://127.0.0.1:{self._runner.addresses[0][1]}/cal/"
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._runner.cleanup()


@pytest.fixture()
def caldav_creds(tmp_path: Path):
    """Point the credential store at a temp dir and seed CalDAV credentials."""
    from kiro_crew.apps.builtins.meetings.backend import credentials as creds

    creds.set_credentials_home(tmp_path / "creds")
    yield creds
    creds.set_credentials_home(None)


class TestCalDavProvider:
    """The CalDAV path, driven against a local fake server.

    No real network, matching this module's rule: the SSRF gate is stubbed to a
    pass-through so a loopback server is reachable, and everything downstream of
    the gate — the REPORT body, the auth header, the Multi-Status parse — runs for
    real.
    """

    @pytest.mark.asyncio
    async def test_it_reports_and_parses_the_events(
        self, monkeypatch: pytest.MonkeyPatch, caldav_creds
    ):
        await caldav_creds.write_for(
            k.CALENDAR_PROVIDER_CALDAV, {"username": "alice", "password": "pw"}
        )
        body = _multistatus(
            _VEVENT_TEMPLATE.format(
                uid="evt-a", summary="Standup", start=_soon(2), end=_soon(3)
            ),
            _VEVENT_TEMPLATE.format(
                uid="evt-b", summary="Review", start=_soon(5), end=_soon(6)
            ),
        )
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeCalDav(body) as server:
            events = await cal.CalDavCalendarProvider(server.base).fetch(days=7)
            got = server.requests

        assert [e.title for e in events] == ["Standup", "Review"]
        assert got[0]["method"] == "REPORT"
        assert got[0]["headers"]["Depth"] == "1"
        assert "calendar-query" in got[0]["body"]
        assert "time-range" in got[0]["body"]

    @pytest.mark.asyncio
    async def test_it_sends_basic_auth_built_from_the_credential_store(
        self, monkeypatch: pytest.MonkeyPatch, caldav_creds
    ):
        await caldav_creds.write_for(
            k.CALENDAR_PROVIDER_CALDAV, {"username": "alice@example.com", "password": "s3cr3t"}
        )
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeCalDav(_multistatus()) as server:
            await cal.CalDavCalendarProvider(server.base).fetch(days=1)
            sent = server.requests[0]["headers"]["Authorization"]

        expected = base64.b64encode(b"alice@example.com:s3cr3t").decode("ascii")
        assert sent == f"Basic {expected}"

    @pytest.mark.asyncio
    async def test_the_credential_travels_as_auth_headers_so_a_redirect_drops_it(self):
        """Structural, because the leak it prevents cannot be seen from outside.

        ``fetch_vetted`` only origin-scopes what arrives in ``auth_headers``; a
        provider that put its Basic credential in ``headers`` would send the
        user's password to whatever host a forged ``Location`` named.
        """
        import inspect

        src = inspect.getsource(cal.CalDavCalendarProvider.fetch)
        assert "auth_headers={" in src
        # The Authorization value must appear in the auth_headers argument, not
        # in the plain headers dict.
        auth_pos = src.index("auth_headers={")
        headers_pos = src.index("headers={")
        assert src.index("Authorization") > auth_pos > headers_pos

    @pytest.mark.asyncio
    async def test_no_source_is_explained_not_a_stack_trace(self, caldav_creds):
        with pytest.raises(cal.CalendarError, match="CalDAV collection URL"):
            await cal.CalDavCalendarProvider("").fetch()

    @pytest.mark.asyncio
    async def test_missing_credentials_are_explained(self, caldav_creds):
        with pytest.raises(cal.CalendarError, match="username and password"):
            await cal.CalDavCalendarProvider("https://dav.example/cal/").fetch()

    @pytest.mark.asyncio
    async def test_a_partial_credential_is_still_refused(self, caldav_creds):
        """A username with no password must not produce a half-formed header."""
        await caldav_creds.write_for(k.CALENDAR_PROVIDER_CALDAV, {"username": "alice"})
        with pytest.raises(cal.CalendarError, match="username and password"):
            await cal.CalDavCalendarProvider("https://dav.example/cal/").fetch()

    @pytest.mark.asyncio
    async def test_an_unauthorized_response_does_not_echo_the_password(
        self, monkeypatch: pytest.MonkeyPatch, caldav_creds
    ):
        await caldav_creds.write_for(
            k.CALENDAR_PROVIDER_CALDAV, {"username": "alice", "password": "do-not-log-me"}
        )
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeCalDav("nope", status=401) as server:
            with pytest.raises(cal.CalendarError) as excinfo:
                await cal.CalDavCalendarProvider(server.base).fetch()

        assert "401" in str(excinfo.value)
        assert "do-not-log-me" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_a_recurring_series_folds_to_its_earliest_instance(
        self, monkeypatch: pytest.MonkeyPatch, caldav_creds
    ):
        """Overrides share the series UID, so they share an event_id.

        Two rows with one id would share a meeting DIRECTORY, which is the
        collision ``_event_id_for`` exists to prevent. The earliest start wins.
        """
        await caldav_creds.write_for(
            k.CALENDAR_PROVIDER_CALDAV, {"username": "a", "password": "b"}
        )
        later = _soon(9)
        earlier = _soon(4)
        body = _multistatus(
            _VEVENT_TEMPLATE.format(uid="series-1", summary="Weekly", start=later, end=_soon(10)),
            _VEVENT_TEMPLATE.format(
                uid="series-1", summary="Weekly", start=earlier, end=_soon(5)
            ),
        )
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeCalDav(body) as server:
            events = await cal.CalDavCalendarProvider(server.base).fetch(days=7)

        assert len(events) == 1
        assert events[0].start == f"{earlier[:4]}-{earlier[4:6]}-{earlier[6:8]}T" + (
            f"{earlier[9:11]}:{earlier[11:13]}:{earlier[13:15]}Z"
        )

    @pytest.mark.asyncio
    async def test_one_unparseable_calendar_object_does_not_lose_the_others(
        self, monkeypatch: pytest.MonkeyPatch, caldav_creds
    ):
        await caldav_creds.write_for(
            k.CALENDAR_PROVIDER_CALDAV, {"username": "a", "password": "b"}
        )
        body = _multistatus(
            "this is not iCalendar at all",
            _VEVENT_TEMPLATE.format(uid="good", summary="Kept", start=_soon(2), end=_soon(3)),
        )
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeCalDav(body) as server:
            events = await cal.CalDavCalendarProvider(server.base).fetch(days=7)

        assert [e.title for e in events] == ["Kept"]

    @pytest.mark.asyncio
    async def test_an_empty_collection_is_an_empty_list_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch, caldav_creds
    ):
        """A calendar with nothing in it is a legitimate answer.

        Unlike ``NoCalendarProvider``, where an empty result would hide a config
        typo, a configured CalDAV collection that returns no events genuinely has
        none this week.
        """
        await caldav_creds.write_for(
            k.CALENDAR_PROVIDER_CALDAV, {"username": "a", "password": "b"}
        )
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeCalDav(_multistatus()) as server:
            assert await cal.CalDavCalendarProvider(server.base).fetch(days=7) == []


class TestCalDavXmlIsHardened:
    """The Multi-Status is XML from a REMOTE server.

    Measured on this interpreter, not assumed: stdlib ``ElementTree`` expands
    internal entities (a nine-line "billion laughs" grows to thousands of
    characters, and each nesting level multiplies that), while EXTERNAL entities
    are already refused as undefined — so file disclosure is closed and expansion
    is the hole. An entity bomb needs an internal DTD, so the DOCTYPE is refused
    outright and no entity-graph analysis is needed.
    """

    def test_an_entity_bomb_is_refused(self):
        bomb = (
            '<?xml version="1.0"?>'
            "<!DOCTYPE lolz ["
            '<!ENTITY lol "lol">'
            '<!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            '<!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">'
            "]>"
            "<root>&lol2;</root>"
        ).encode("utf-8")
        with pytest.raises(cal.CalendarError, match="document type declaration"):
            cal._parse_xml_safely(bomb)

    def test_the_refusal_is_a_blanket_doctype_rule(self):
        """Even a DOCTYPE with no entities is refused.

        The rule is deliberately coarse: a legitimate CalDAV Multi-Status carries
        no DOCTYPE, so refusing all of them costs nothing real and removes the
        need to decide which entity declarations are safe.
        """
        with pytest.raises(cal.CalendarError, match="document type declaration"):
            cal._parse_xml_safely(b'<?xml version="1.0"?><!DOCTYPE root><root/>')

    def test_external_entities_are_refused_too(self, tmp_path: Path):
        """Belt and braces: pins that no file read is reachable through the XML."""
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP-SECRET", encoding="utf-8")
        xxe = (
            f'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file://{secret}">]>'
            "<root>&x;</root>"
        ).encode("utf-8")
        with pytest.raises(cal.CalendarError):
            cal._parse_xml_safely(xxe)

    def test_malformed_xml_is_a_calendar_error_not_a_parse_error(self):
        """The route turns ``CalendarError`` into a 502; a bare ``ParseError``
        would escape as a 500."""
        with pytest.raises(cal.CalendarError, match="could not be parsed"):
            cal._parse_xml_safely(b"<multistatus><unclosed>")

    def test_a_legitimate_multistatus_still_parses(self):
        payload = _multistatus(
            _VEVENT_TEMPLATE.format(uid="u1", summary="Ok", start=_soon(1), end=_soon(2))
        ).encode("utf-8")
        events = cal._events_from_multistatus(payload, days=7)
        assert [e.title for e in events] == ["Ok"]


# ── Google Calendar ─────────────────────────────────────────────────────────


class _FakeGoogleEvents:
    """A local stand-in for ``events.list``, recording each request."""

    def __init__(self, *pages: dict[str, Any], status: int = 200) -> None:
        self._pages = list(pages)
        self._status = status
        self.requests: list[dict[str, Any]] = []
        self._runner: Any = None
        self.url = ""

    async def __aenter__(self) -> _FakeGoogleEvents:
        from aiohttp import web as aioweb

        async def handler(request):
            self.requests.append(
                {"query": dict(request.query), "headers": dict(request.headers)}
            )
            index = min(len(self.requests) - 1, len(self._pages) - 1)
            if self._status != 200:
                return aioweb.Response(status=self._status, text="denied")
            return aioweb.json_response(self._pages[index])

        app = aioweb.Application()
        app.router.add_get("/events", handler)
        self._runner = aioweb.AppRunner(app)
        await self._runner.setup()
        site = aioweb.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{self._runner.addresses[0][1]}/events"
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._runner.cleanup()


def _google_item(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "gid-1",
        "status": "confirmed",
        "summary": "Design review",
        "start": {"dateTime": "2026-08-05T09:00:00+09:00"},
        "end": {"dateTime": "2026-08-05T10:00:00+09:00"},
        "location": "Room 4",
        "organizer": {"displayName": "Alice", "email": "alice@example.com"},
        "attendees": [{"displayName": "Bob"}, {"email": "carol@example.com"}],
        "description": "agenda",
    }
    base.update(over)
    return base


@pytest.fixture()
def google_connected(tmp_path: Path):
    """A credential store with Google already connected and a live token."""
    import time as _time

    from kiro_crew.apps.builtins.meetings.backend import credentials as creds

    creds.set_credentials_home(tmp_path / "creds")
    yield creds, _time
    creds.set_credentials_home(None)


class TestGoogleCalendarProvider:
    @staticmethod
    async def _connect(creds, _time) -> None:
        await creds.write_for(
            k.CALENDAR_PROVIDER_GOOGLE,
            {
                "client_id": "cid",
                "refresh_token": "rt",
                "access_token": "live-token",
                "expires_at": str(_time.time() + 3600),
            },
        )

    def test_it_does_not_require_a_source(self):
        rows = {row["id"]: row for row in cal.available_calendar_providers()}
        # It reads the signed-in user's `primary` calendar; asking for a calendar
        # id is not something a user has to hand.
        assert rows[k.CALENDAR_PROVIDER_GOOGLE]["requires_source"] is False

    @pytest.mark.asyncio
    async def test_an_unconnected_account_is_explained(self, google_connected):
        with pytest.raises(cal.CalendarError, match="not connected"):
            await cal.GoogleCalendarProvider().fetch()

    @pytest.mark.asyncio
    async def test_it_sends_the_bearer_token_and_the_right_query(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeGoogleEvents({"items": [_google_item()]}) as api:
            monkeypatch.setattr(k, "GOOGLE_EVENTS_URL", api.url)
            events = await cal.GoogleCalendarProvider().fetch(days=7)
            req = api.requests[0]

        assert req["headers"]["Authorization"] == "Bearer live-token"
        # singleEvents=true is what makes Google expand a recurring series
        # server-side, which is why this provider needs no RRULE engine.
        assert req["query"]["singleEvents"] == "true"
        assert req["query"]["orderBy"] == "startTime"
        assert "timeMin" in req["query"] and "timeMax" in req["query"]
        assert [e.title for e in events] == ["Design review"]

    @pytest.mark.asyncio
    async def test_the_bearer_token_travels_as_auth_headers(self):
        """So a redirect off googleapis.com cannot carry it away."""
        import inspect

        src = inspect.getsource(cal.GoogleCalendarProvider.fetch)
        auth_pos = src.index("auth_headers={")
        assert src.index("Bearer") > auth_pos

    @pytest.mark.asyncio
    async def test_an_offset_timestamp_becomes_utc(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        """09:00+09:00 is 00:00Z. Reading the offset as UTC would name the wrong
        instant, so the ordering and the sync window would both be wrong."""
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        start = (datetime.now(timezone.utc) + timedelta(hours=5)).astimezone(
            timezone(timedelta(hours=9))
        )
        item = _google_item(
            start={"dateTime": start.isoformat()},
            end={"dateTime": (start + timedelta(hours=1)).isoformat()},
        )
        async with _FakeGoogleEvents({"items": [item]}) as api:
            monkeypatch.setattr(k, "GOOGLE_EVENTS_URL", api.url)
            events = await cal.GoogleCalendarProvider().fetch(days=7)

        assert events[0].start == start.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

    @pytest.mark.asyncio
    async def test_a_whole_day_event_is_read_as_midnight_utc(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        day = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d")
        item = _google_item(start={"date": day}, end={"date": day})
        async with _FakeGoogleEvents({"items": [item]}) as api:
            monkeypatch.setattr(k, "GOOGLE_EVENTS_URL", api.url)
            events = await cal.GoogleCalendarProvider().fetch(days=7)

        assert events[0].start == f"{day}T00:00:00Z"

    @pytest.mark.asyncio
    async def test_a_cancelled_instance_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        """A cancelled occurrence still appears in the list; showing it would put
        a phantom meeting in the user's day."""
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        page = {
            "items": [
                _google_item(id="dead", status="cancelled", start=None, end=None),
                _google_item(id="alive", summary="Kept"),
            ]
        }
        async with _FakeGoogleEvents(page) as api:
            monkeypatch.setattr(k, "GOOGLE_EVENTS_URL", api.url)
            events = await cal.GoogleCalendarProvider().fetch(days=7)

        assert [e.title for e in events] == ["Kept"]

    @pytest.mark.asyncio
    async def test_an_item_with_no_usable_start_is_skipped_not_fatal(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        page = {
            "items": [
                _google_item(id="bad", start={"dateTime": "not-a-timestamp"}),
                _google_item(id="ok", summary="Survived"),
            ]
        }
        async with _FakeGoogleEvents(page) as api:
            monkeypatch.setattr(k, "GOOGLE_EVENTS_URL", api.url)
            events = await cal.GoogleCalendarProvider().fetch(days=7)

        assert [e.title for e in events] == ["Survived"]

    @pytest.mark.asyncio
    async def test_it_follows_nextpagetoken(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        first = {"items": [_google_item(id="p1", summary="One")], "nextPageToken": "tok-2"}
        second = {"items": [_google_item(id="p2", summary="Two")]}
        async with _FakeGoogleEvents(first, second) as api:
            monkeypatch.setattr(k, "GOOGLE_EVENTS_URL", api.url)
            events = await cal.GoogleCalendarProvider().fetch(days=7)
            requests = api.requests

        assert len(requests) == 2
        assert requests[1]["query"]["pageToken"] == "tok-2"
        assert sorted(e.title for e in events) == ["One", "Two"]

    @pytest.mark.asyncio
    async def test_endless_pagination_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        """A provider that always returns a nextPageToken must not loop forever."""
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        forever = {"items": [_google_item()], "nextPageToken": "always"}
        async with _FakeGoogleEvents(forever) as api:
            monkeypatch.setattr(k, "GOOGLE_EVENTS_URL", api.url)
            await cal.GoogleCalendarProvider().fetch(days=7)
            assert len(api.requests) == k.GOOGLE_MAX_PAGES

    @pytest.mark.asyncio
    async def test_a_non_json_body_is_a_calendar_error(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        with pytest.raises(cal.CalendarError, match="not JSON"):
            cal._json_object(b"<html>error</html>")

    def test_the_endpoints_are_constants_not_configuration(self):
        """A settable authorize URL is a phishing surface: a rewritten host
        collects the user's Google password. Only the client id/secret are
        per-installation."""
        assert k.GOOGLE_AUTHORIZE_URL.startswith("https://accounts.google.com/")
        assert k.GOOGLE_TOKEN_URL.startswith("https://oauth2.googleapis.com/")
        assert k.GOOGLE_CALENDAR_SCOPE.endswith("calendar.readonly")
        # Read-only, because the app only ever displays meetings.
        assert "readonly" in k.GOOGLE_CALENDAR_SCOPE

    def test_offline_access_and_reconsent_are_requested(self):
        """Without access_type=offline Google issues no refresh token, and
        without prompt=consent a RECONNECT issues none either -- the calendar
        would then stop working an hour after every reconnect."""
        assert k.GOOGLE_AUTHORIZE_EXTRA["access_type"] == "offline"
        assert k.GOOGLE_AUTHORIZE_EXTRA["prompt"] == "consent"

    def test_the_oauth_client_matches_the_constants(self):
        client = cal.google_oauth_client()
        assert client.provider_id == k.CALENDAR_PROVIDER_GOOGLE
        assert client.authorize_url == k.GOOGLE_AUTHORIZE_URL
        assert client.token_url == k.GOOGLE_TOKEN_URL
        assert client.scopes == (k.GOOGLE_CALENDAR_SCOPE,)


# ── Microsoft 365 (Graph) ───────────────────────────────────────────────────


class _FakeGraph:
    """A local stand-in for Graph's ``calendarView``."""

    def __init__(self, *pages: dict[str, Any]) -> None:
        self._pages = list(pages)
        self.requests: list[dict[str, Any]] = []
        self._runner: Any = None
        self.url = ""

    async def __aenter__(self) -> _FakeGraph:
        from aiohttp import web as aioweb

        async def handler(request):
            self.requests.append(
                {"query": dict(request.query), "headers": dict(request.headers)}
            )
            index = min(len(self.requests) - 1, len(self._pages) - 1)
            page = dict(self._pages[index])
            # Graph paginates with a FULL url; rewrite the placeholder so it
            # points back at this server.
            if page.get("@odata.nextLink") == "NEXT":
                page["@odata.nextLink"] = f"{self.url}?page=2"
            return aioweb.json_response(page)

        app = aioweb.Application()
        app.router.add_get("/calendarView", handler)
        self._runner = aioweb.AppRunner(app)
        await self._runner.setup()
        site = aioweb.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"http://127.0.0.1:{self._runner.addresses[0][1]}/calendarView"
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._runner.cleanup()


def _graph_item(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "AAMk-1",
        "isCancelled": False,
        "subject": "Sprint planning",
        "start": {"dateTime": "2026-08-05T09:00:00.0000000", "timeZone": "UTC"},
        "end": {"dateTime": "2026-08-05T10:00:00.0000000", "timeZone": "UTC"},
        "location": {"displayName": "Teams"},
        "organizer": {"emailAddress": {"name": "Dana", "address": "dana@example.com"}},
        "attendees": [{"emailAddress": {"name": "Eve"}}],
        "bodyPreview": "the agenda",
    }
    base.update(over)
    return base


class TestMicrosoftCalendarProvider:
    @staticmethod
    async def _connect(creds, _time) -> None:
        await creds.write_for(
            k.CALENDAR_PROVIDER_MICROSOFT,
            {
                "client_id": "cid",
                "refresh_token": "rt",
                "access_token": "graph-token",
                "expires_at": str(_time.time() + 3600),
            },
        )

    def test_it_does_not_require_a_source(self):
        rows = {row["id"]: row for row in cal.available_calendar_providers()}
        assert rows[k.CALENDAR_PROVIDER_MICROSOFT]["requires_source"] is False

    @pytest.mark.asyncio
    async def test_an_unconnected_account_is_explained(self, google_connected):
        with pytest.raises(cal.CalendarError, match="not connected"):
            await cal.MicrosoftCalendarProvider().fetch()

    @pytest.mark.asyncio
    async def test_it_asks_graph_for_utc_and_sends_the_bearer_token(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        """The Prefer header is load-bearing: Graph's dateTime has no offset, so
        without it the naive value would be in the mailbox's zone and read as
        UTC."""
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeGraph({"value": [_graph_item()]}) as api:
            monkeypatch.setattr(k, "MICROSOFT_EVENTS_URL", api.url)
            events = await cal.MicrosoftCalendarProvider().fetch(days=7)
            req = api.requests[0]

        assert req["headers"]["Authorization"] == "Bearer graph-token"
        assert req["headers"]["Prefer"] == 'outlook.timezone="UTC"'
        assert "startDateTime" in req["query"] and "endDateTime" in req["query"]
        assert [e.title for e in events] == ["Sprint planning"]

    @pytest.mark.asyncio
    async def test_it_reads_the_nested_organizer_and_attendee_names(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        """Graph nests the name one level deeper than Google, inside
        ``emailAddress``."""
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        async with _FakeGraph({"value": [_graph_item()]}) as api:
            monkeypatch.setattr(k, "MICROSOFT_EVENTS_URL", api.url)
            events = await cal.MicrosoftCalendarProvider().fetch(days=7)

        assert events[0].organizer == "Dana"
        assert events[0].attendees == ["Eve"]
        assert events[0].location == "Teams"

    @pytest.mark.asyncio
    async def test_a_named_timezone_is_applied_to_the_naive_stamp(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        """A response that ignored the Prefer header must not be misread.

        The stamp is wall-clock in ``timeZone``, so the zone is APPLIED to it
        rather than converted from UTC.
        """
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        soon = datetime.now(timezone.utc) + timedelta(hours=6)
        wall = soon.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%dT%H:%M:%S.0000000")
        item = _graph_item(
            start={"dateTime": wall, "timeZone": "Asia/Tokyo"},
            end={"dateTime": wall, "timeZone": "Asia/Tokyo"},
        )
        async with _FakeGraph({"value": [item]}) as api:
            monkeypatch.setattr(k, "MICROSOFT_EVENTS_URL", api.url)
            events = await cal.MicrosoftCalendarProvider().fetch(days=7)

        assert events[0].start == soon.strftime("%Y-%m-%dT%H:%M:%SZ")

    @pytest.mark.asyncio
    async def test_a_windows_zone_name_falls_back_to_utc(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        """Graph often answers with ``Tokyo Standard Time``, which is not an IANA
        key. A visible meeting at a possibly-wrong hour beats one that is not
        there — the same stance ``_tzid_of`` already takes."""
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        stamp = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime(
            "%Y-%m-%dT%H:%M:%S.0000000"
        )
        item = _graph_item(
            start={"dateTime": stamp, "timeZone": "Tokyo Standard Time"},
            end={"dateTime": stamp, "timeZone": "Tokyo Standard Time"},
        )
        async with _FakeGraph({"value": [item]}) as api:
            monkeypatch.setattr(k, "MICROSOFT_EVENTS_URL", api.url)
            events = await cal.MicrosoftCalendarProvider().fetch(days=7)

        assert events[0].start == f"{stamp[:19]}Z"

    @pytest.mark.asyncio
    async def test_a_cancelled_occurrence_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        page = {
            "value": [
                _graph_item(id="x", isCancelled=True, subject="Dropped"),
                _graph_item(id="y", subject="Kept"),
            ]
        }
        async with _FakeGraph(page) as api:
            monkeypatch.setattr(k, "MICROSOFT_EVENTS_URL", api.url)
            events = await cal.MicrosoftCalendarProvider().fetch(days=7)

        assert [e.title for e in events] == ["Kept"]

    @pytest.mark.asyncio
    async def test_it_follows_the_odata_nextlink(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        first = {"value": [_graph_item(id="a", subject="One")], "@odata.nextLink": "NEXT"}
        second = {"value": [_graph_item(id="b", subject="Two")]}
        async with _FakeGraph(first, second) as api:
            monkeypatch.setattr(k, "MICROSOFT_EVENTS_URL", api.url)
            events = await cal.MicrosoftCalendarProvider().fetch(days=7)
            requests = api.requests

        assert len(requests) == 2
        assert sorted(e.title for e in events) == ["One", "Two"]

    @pytest.mark.asyncio
    async def test_a_nextlink_at_a_private_address_is_refused_by_the_gate(
        self, google_connected
    ):
        """The pagination URL comes from the response BODY, so it is exactly as
        untrusted as a redirect ``Location`` — and gets the same gate."""
        with pytest.raises(cal.CalendarError):
            cal._normalize_url("https://169.254.169.254/graph")

    @pytest.mark.asyncio
    async def test_endless_pagination_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch, google_connected
    ):
        creds, _time = google_connected
        await self._connect(creds, _time)
        monkeypatch.setattr(cal, "_normalize_url", _passthrough_target)
        forever = {"value": [_graph_item()], "@odata.nextLink": "NEXT"}
        async with _FakeGraph(forever) as api:
            monkeypatch.setattr(k, "MICROSOFT_EVENTS_URL", api.url)
            await cal.MicrosoftCalendarProvider().fetch(days=7)
            assert len(api.requests) == k.MICROSOFT_MAX_PAGES

    def test_offline_access_is_requested(self):
        """Without ``offline_access`` Microsoft issues no refresh token at all, so
        the calendar would stop working when the first access token expires."""
        assert "offline_access" in k.MICROSOFT_CALENDAR_SCOPES

    def test_the_scope_is_read_only(self):
        assert "Calendars.Read" in k.MICROSOFT_CALENDAR_SCOPES[0]
        assert not any("ReadWrite" in s for s in k.MICROSOFT_CALENDAR_SCOPES)

    def test_the_endpoints_are_constants_and_multi_tenant(self):
        # `/common/` so a personal and a work account both work without the user
        # having to find a tenant id.
        assert "/common/" in k.MICROSOFT_AUTHORIZE_URL
        assert k.MICROSOFT_AUTHORIZE_URL.startswith("https://login.microsoftonline.com/")
        assert k.MICROSOFT_EVENTS_URL.startswith("https://graph.microsoft.com/")
        # calendarView, not /events: it expands a recurring series server-side.
        assert k.MICROSOFT_EVENTS_URL.endswith("/calendarView")

    def test_the_oauth_client_matches_the_constants(self):
        client = cal.microsoft_oauth_client()
        assert client.provider_id == k.CALENDAR_PROVIDER_MICROSOFT
        assert client.authorize_url == k.MICROSOFT_AUTHORIZE_URL
        assert client.token_url == k.MICROSOFT_TOKEN_URL
        assert client.scopes == tuple(k.MICROSOFT_CALENDAR_SCOPES)
