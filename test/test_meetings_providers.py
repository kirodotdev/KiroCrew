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

import itertools
import json
import socket
import ssl
import threading
import types
from concurrent import futures
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from meetings_helpers import (  # noqa: F401
    reset_module_state_fixture,
    root_fixture,
)
from yarl import URL

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend.providers import calendar as cal
from kiro_crew.apps.builtins.meetings.backend.providers import tasks as taskprov


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
        assert events[0].event_id == "abc-123"
        assert events[0].location == "Room 1"

    def test_skips_events_outside_the_horizon(self):
        far = (datetime.now(timezone.utc) + timedelta(days=60)).strftime("%Y%m%dT%H%M%SZ")
        old = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y%m%dT%H%M%SZ")
        text = _ics(
            _vevent(UID="soon", SUMMARY="Soon", DTSTART=_stamp(1)),
            _vevent(UID="far", SUMMARY="Far", DTSTART=far),
            _vevent(UID="old", SUMMARY="Old", DTSTART=old),
        )
        assert [e.event_id for e in cal.parse_ics(text, days=7)] == ["soon"]

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
        assert event.event_id.startswith("ics-")

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
        assert [e.event_id for e in cal.parse_ics(text)] == ["sooner", "later"]

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

    def test_port_is_taken_from_the_url(self, public_dns: None):
        assert cal._normalize_url("https://example.test:8443/cal.ics").port == 8443

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
            for line in inspect.getsource(cal.IcsCalendarProvider._fetch_url).splitlines()
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
        monkeypatch.setattr(cal, "_refuse_private_address", lambda _addr: None)

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


class TestCalendarRegistry:
    def test_defaults_registered(self):
        ids = {row["id"] for row in cal.available_calendar_providers()}
        assert ids == {k.CALENDAR_PROVIDER_NONE, k.CALENDAR_PROVIDER_ICS}

    def test_ics_declares_it_needs_a_source(self):
        rows = {row["id"]: row for row in cal.available_calendar_providers()}
        assert rows[k.CALENDAR_PROVIDER_ICS]["requires_source"] is True
        assert rows[k.CALENDAR_PROVIDER_NONE]["requires_source"] is False

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
        """Pins the flag itself: without it, no per-hop validation can run."""
        import inspect

        src = inspect.getsource(cal.IcsCalendarProvider._fetch_url)
        assert "allow_redirects=False" in src
        assert "_REDIRECT_STATUSES" in src

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

        src = inspect.getsource(cal.IcsCalendarProvider._fetch_url)
        assert "ICS_MAX_REDIRECTS" in src
        assert "redirected too many times" in src


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
