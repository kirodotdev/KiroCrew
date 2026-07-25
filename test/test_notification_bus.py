"""Tests for the notification bus core (Phase 1 of the local notification bus RFC)."""

import pytest

from kiro_crew.notifications.bus import (
    SYSTEM_CHANNELS,
    NotificationBus,
    NotificationPayload,
    NotificationValidationError,
    normalize_note,
    payload_from_legacy,
)


def _make_bus() -> tuple[NotificationBus, list[dict]]:
    delivered: list[dict] = []
    bus = NotificationBus(sink=delivered.append)
    return bus, delivered


# ── Payload validation ──


class TestPayloadValidation:
    def test_valid_minimal_payload(self):
        p = NotificationPayload(source="system", channel="system.cron", title="t", body="b")
        p.validate()  # must not raise

    @pytest.mark.parametrize("field_name", ["source", "channel", "title"])
    def test_missing_required_field_rejected(self, field_name):
        kwargs = {"source": "system", "channel": "system.cron", "title": "t", "body": "b"}
        kwargs[field_name] = ""
        with pytest.raises(NotificationValidationError):
            NotificationPayload(**kwargs).validate()

    def test_bad_priority_rejected(self):
        p = NotificationPayload(
            source="system", channel="system.cron", title="t", body="b", priority="urgent"
        )
        with pytest.raises(NotificationValidationError, match="priority"):
            p.validate()

    def test_non_positive_ttl_rejected(self):
        p = NotificationPayload(
            source="system", channel="system.cron", title="t", body="b", ttl=0
        )
        with pytest.raises(NotificationValidationError, match="ttl"):
            p.validate()

    def test_action_without_id_rejected(self):
        p = NotificationPayload(
            source="system",
            channel="system.approval",
            title="t",
            body="b",
            actions=[{"label": "Approve"}],
        )
        with pytest.raises(NotificationValidationError, match="action"):
            p.validate()

    def test_external_url_rejected(self):
        p = NotificationPayload(
            source="system",
            channel="system.cron",
            title="t",
            body="b",
            url="https://evil.example.com/",
        )
        with pytest.raises(NotificationValidationError, match="url"):
            p.validate()

    def test_internal_url_accepted(self):
        p = NotificationPayload(
            source="system", channel="system.cron", title="t", body="b", url="/schedule"
        )
        p.validate()  # must not raise

    def test_protocol_relative_url_rejected(self):
        # "//evil.example.com" starts with "/" but resolves to an external
        # origin in a browser — must be rejected like any external URL.
        p = NotificationPayload(
            source="system",
            channel="system.cron",
            title="t",
            body="b",
            url="//evil.example.com/x",
        )
        with pytest.raises(NotificationValidationError, match="url"):
            p.validate()

    def test_backslash_url_rejected(self):
        # Browsers normalize "\" to "/" per the WHATWG URL spec, so
        # "/\evil.example.com" is equivalent to the protocol-relative
        # "//evil.example.com" — must be rejected too.
        p = NotificationPayload(
            source="system",
            channel="system.cron",
            title="t",
            body="b",
            url="/\\evil.example.com/x",
        )
        with pytest.raises(NotificationValidationError, match="url"):
            p.validate()

    @pytest.mark.parametrize("stripped_char", ["\t", "\n", "\r"])
    def test_whatwg_stripped_chars_in_url_rejected(self, stripped_char):
        # WHATWG URL parsing removes ASCII tab/newline/CR before
        # interpreting the URL, so "/\t/evil.example.com" would become the
        # protocol-relative "//evil.example.com" — must be rejected too.
        p = NotificationPayload(
            source="system",
            channel="system.cron",
            title="t",
            body="b",
            url=f"/{stripped_char}/evil.example.com/x",
        )
        with pytest.raises(NotificationValidationError, match="url"):
            p.validate()


# ── Channel registry ──


class TestChannelRegistry:
    def test_system_channels_registered_by_default(self):
        bus, _ = _make_bus()
        for channel in SYSTEM_CHANNELS:
            assert bus.is_registered(channel)

    def test_unregistered_channel_rejected(self):
        bus, delivered = _make_bus()
        p = NotificationPayload(
            source="app:oncall-radar", channel="oncall-radar.tickets", title="t", body="b"
        )
        with pytest.raises(NotificationValidationError, match="unregistered"):
            bus.push(p)
        assert delivered == []

    def test_registered_app_channel_accepted(self):
        bus, delivered = _make_bus()
        bus.register_channel("oncall-radar.tickets")
        p = NotificationPayload(
            source="app:oncall-radar", channel="oncall-radar.tickets", title="t", body="b"
        )
        note = bus.push(p)
        assert delivered == [note]
        assert note["source"] == "app:oncall-radar"
        assert note["kind"] == "tickets"

    def test_register_channel_bad_priority_rejected(self):
        bus, _ = _make_bus()
        with pytest.raises(NotificationValidationError):
            bus.register_channel("app.x", default_priority="loud")

    def test_system_channel_cannot_be_unregistered(self):
        bus, _ = _make_bus()
        bus.unregister_channel("system.cron")
        assert bus.is_registered("system.cron")

    def test_app_channel_unregistered(self):
        bus, _ = _make_bus()
        bus.register_channel("app.x")
        bus.unregister_channel("app.x")
        assert not bus.is_registered("app.x")


# ── Priority resolution ──


class TestPriority:
    def test_channel_default_priority_applied(self):
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(source="system", channel="system.cron", title="t", body="b")
        )
        assert note["priority"] == "default"

    def test_approval_defaults_to_critical(self):
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(source="system", channel="system.approval", title="t", body="b")
        )
        assert note["priority"] == "critical"

    def test_subagent_defaults_to_passive(self):
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(source="system", channel="system.subagent", title="t", body="b")
        )
        assert note["priority"] == "passive"

    def test_explicit_priority_wins_over_channel_default(self):
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(
                source="system",
                channel="system.cron",
                title="t",
                body="b",
                priority="critical",
            )
        )
        assert note["priority"] == "critical"


# ── Note shape (backward compatibility) ──


class TestNoteShape:
    def test_note_carries_legacy_kind(self):
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(source="system", channel="system.heartbeat", title="t", body="b")
        )
        assert note["kind"] == "heartbeat"
        assert note["channel"] == "system.heartbeat"
        assert "ts" in note

    def test_meta_merges_flat_without_clobbering_reserved(self):
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(
                source="system",
                channel="system.cron",
                title="t",
                body="b",
                meta={"job_id": "abc123", "title": "EVIL", "channel": "EVIL"},
            )
        )
        assert note["job_id"] == "abc123"
        assert note["title"] == "t"  # reserved key not clobbered by meta
        assert note["channel"] == "system.cron"

    def test_meta_cannot_clobber_a_builder_set_field(self):
        # Default-safe guard: any key the builder already wrote to the note is
        # off-limits to meta by construction (the "key in note" check), so a new
        # schema field is protected without having to be re-listed in a denylist.
        # group_key is a builder-set optional field that is not underscore-
        # prefixed.
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(
                source="system",
                channel="system.cron",
                title="t",
                body="b",
                group_key="real-group",
                meta={"group_key": "EVIL", "job_id": "j1"},
            )
        )
        assert note["group_key"] == "real-group"
        assert note["job_id"] == "j1"

    def test_meta_cannot_smuggle_unset_schema_fields(self):
        # meta={"url": ...} must not inject an unvalidated URL into the note
        # when payload.url is None (validation only runs on payload fields).
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(
                source="system",
                channel="system.cron",
                title="t",
                body="b",
                meta={"url": "https://evil.example.com", "ttl": -1, "job_id": "j1"},
            )
        )
        assert "url" not in note
        assert "ttl" not in note
        assert note["job_id"] == "j1"

    def test_meta_cannot_smuggle_broadcast_envelope_keys(self):
        # DashboardState._broadcast branches on note["_type"] (and reads
        # companion keys like slot/role/content once set) to decide the WS
        # frame. Caller-supplied meta must not be able to inject "_type" (or any
        # underscore-prefixed key) and hijack the envelope.
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(
                source="system",
                channel="system.cron",
                title="t",
                body="b",
                meta={
                    "_type": "slot_update",
                    "slot": "victim",
                    "content": "spoofed",
                    "job_id": "j1",
                },
            )
        )
        assert "_type" not in note
        assert note.get("_type", "notification") == "notification"
        assert note["job_id"] == "j1"  # non-private meta still merges

    def test_meta_cannot_preset_status_fields(self):
        # 'silenced'/'acked' are sink/frontend-owned; a caller must not
        # pre-suppress or pre-ack a note it is pushing.
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(
                source="system",
                channel="system.cron",
                title="t",
                body="b",
                meta={"silenced": True, "acked": True, "job_id": "j1"},
            )
        )
        assert "silenced" not in note
        assert "acked" not in note
        assert note["job_id"] == "j1"

    def test_optional_fields_omitted_when_unset(self):
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(source="system", channel="system.cron", title="t", body="b")
        )
        for key in ("group_key", "actions", "url", "icon", "ttl"):
            assert key not in note

    def test_optional_fields_present_when_set(self):
        bus, _ = _make_bus()
        note = bus.push(
            NotificationPayload(
                source="system",
                channel="system.approval",
                title="t",
                body="b",
                group_key="g1",
                actions=[{"id": "approve", "label": "Approve"}],
                url="/chat",
                icon="bell",
                ttl=60,
            )
        )
        assert note["group_key"] == "g1"
        assert note["actions"] == [{"id": "approve", "label": "Approve"}]
        assert note["url"] == "/chat"
        assert note["icon"] == "bell"
        assert note["ttl"] == 60


# ── Legacy adapter ──


class TestPayloadFromLegacy:
    @pytest.mark.parametrize(
        "kind", ["cron", "heartbeat", "hook", "agent", "approval", "subagent", "taskrunner"]
    )
    def test_known_kind_maps_to_system_channel(self, kind):
        p = payload_from_legacy(kind, "t", "b")
        assert p.channel == f"system.{kind}"
        assert p.source == "system"

    def test_unknown_kind_falls_back(self):
        p = payload_from_legacy("mystery", "t", "b")
        assert p.channel == "system.agent"

    def test_unknown_kind_preserved_in_note(self):
        # Regression: notify("secretary_alert", ...) must keep its kind for
        # frontend filters even though it delivers on the fallback channel.
        bus, delivered = _make_bus()
        bus.push(payload_from_legacy("secretary_alert", "t", "b"))
        assert delivered[0]["kind"] == "secretary_alert"
        assert delivered[0]["channel"] == "system.agent"

    def test_oversized_body_truncated_not_dropped(self):
        # Regression: legacy notify() never dropped — cron results and
        # send_message text can exceed the schema body cap.
        bus, delivered = _make_bus()
        bus.push(payload_from_legacy("cron", "t", "x" * 30000))
        assert len(delivered) == 1
        assert len(delivered[0]["body"]) == 20000
        assert delivered[0]["body"].endswith("…")

    def test_oversized_title_truncated_not_dropped(self):
        bus, delivered = _make_bus()
        bus.push(payload_from_legacy("cron", "t" * 600, "b"))
        assert len(delivered) == 1
        assert len(delivered[0]["title"]) == 500

    def test_empty_title_gets_placeholder(self):
        # Regression: send_message with title="" must still deliver.
        bus, delivered = _make_bus()
        bus.push(payload_from_legacy("agent", "", "b"))
        assert len(delivered) == 1
        assert delivered[0]["title"] == "Notification"

    def test_non_string_inputs_coerced_not_raised(self):
        # Regression: legacy notify() never raised — a None/int body must be
        # coerced, not TypeError out of the adapter.
        bus, delivered = _make_bus()
        bus.push(payload_from_legacy("agent", None, 42))  # type: ignore[arg-type]
        assert len(delivered) == 1
        assert delivered[0]["title"] == "Notification"
        assert delivered[0]["body"] == "42"

    def test_meta_copied_not_aliased(self):
        meta = {"job_id": "j1"}
        p = payload_from_legacy("cron", "t", "b", meta)
        meta["job_id"] = "changed"
        assert p.meta["job_id"] == "j1"


# ── Legacy row normalization (JSONL load path) ──


class TestNormalizeNote:
    def test_legacy_row_gains_v2_fields(self):
        note = normalize_note({"kind": "cron", "title": "t", "body": "b", "ts": "2026-01-01"})
        assert note["channel"] == "system.cron"
        assert note["source"] == "system"
        assert note["priority"] == "default"

    def test_legacy_approval_row_gets_critical(self):
        note = normalize_note({"kind": "approval", "title": "t", "body": "b"})
        assert note["priority"] == "critical"

    def test_legacy_unknown_kind_falls_back(self):
        note = normalize_note({"kind": "mystery", "title": "t", "body": "b"})
        assert note["channel"] == "system.agent"

    def test_v2_row_unchanged(self):
        row = {
            "kind": "tickets",
            "source": "app:oncall-radar",
            "channel": "oncall-radar.tickets",
            "priority": "passive",
            "title": "t",
            "body": "b",
        }
        note = normalize_note(dict(row))
        assert note == row

    def test_row_without_kind_defaults_to_agent(self):
        note = normalize_note({"title": "t", "body": "b"})
        assert note["channel"] == "system.agent"
