"""``dashboard.widget_snapshot``: nothing the dashboard page would redact leaves in a widget.

The widget routes are a new egress boundary (mobile widgets design, section 6), so the
snapshot applies the page's own pass, ``member_dashboard.page_safe_for``, to every
resolved value before a block reads it, and again to the finished body. These tests put
a masked key, a worker session key, a crew key and a credential ``redact_via_context``
rewrites into every block type and nesting a widget can carry, and check that none of
them is anywhere in the serialized snapshot.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import pytest

from kiro_crew.dashboard import widget_snapshot as ws
from kiro_crew.dashboard.handlers import member_dashboard as md
from kiro_crew.dashboard_feed import FieldRead
from kiro_crew.platform import context as platform_context
from kiro_crew.platform.context import redact_via_context
from kiro_crew.testing.synthetic_credentials import github_classic

SLUG = "pebble"
CREW = "Pebble"
NOW = datetime(2026, 11, 18, 0, 30, 0, tzinfo=timezone.utc)
BOT = {"slug": SLUG, "member": CREW, "name": "Pebble"}

#: A credential the redactor rewrites. Its shape fits the row-key grammar
#: (``[A-Za-z0-9_]{1,64}``), so a widget may legally name it as ``item_text``.
TOKEN = github_classic(seed=71)
#: The value of a worker's session key and of the panel's ownership digest.
SESSION_KEY = "chat-worker-sentinel-5d1c"
CREW_KEY = "crewkey-digest-sentinel-9a7e"
#: The masked key names themselves.
MASKED_NAMES = ("worker_session_key", "crew_key")


def _types() -> dict[str, Any]:
    return {
        "headline": {"type": "text", "source": {"agentic": True}},
        "state": {"type": "text", "source": {"agentic": True}},
        "count": {"type": "number", "source": {"agentic": True}},
        "done": {"type": "number", "source": {"agentic": True}},
        "rows": {"type": "array", "source": {"fold": "waiting", "path": "items"}},
        "keyed_rows": {"type": "array", "source": {"fold": "waiting", "path": "keyed"}},
        "events": {"type": "array", "source": {"fold": "panel", "path": "events"}},
        "series": {"type": "array", "source": {"fold": "workstreams", "path": "series"}},
    }


def _widget(**over: Any) -> dict[str, Any]:
    widget: dict[str, Any] = {
        "id": "secrets",
        "title": "Secrets",
        "sizes": ["small", "medium", "large"],
        "blocks": [
            {"type": "title", "field": "headline"},
            {"type": "status", "label": "State", "field": "state"},
            {"type": "metric", "label": "Count", "field": "count", "format": "integer"},
            {"type": "progress", "label": "Done", "field": "done", "total": 5},
            {
                "type": "list",
                "label": "Rows",
                "field": "rows",
                "item_text": "title",
                "item_detail": "worker_session_key",
                "max_items": 6,
            },
            # A row key that is itself a credential: the page renames it, so the
            # block finds no text in the row and draws nothing.
            {
                "type": "list",
                "label": "Keyed",
                "field": "keyed_rows",
                "item_text": TOKEN,
                "max_items": 6,
            },
            {
                "type": "list",
                "label": "Events",
                "field": "events",
                "item_text": "text",
                "item_detail": "crew_key",
                "max_items": 6,
            },
            {
                "type": "sparkline",
                "label": "Series",
                "field": "series",
                "point": "n",
                "max_points": 6,
            },
        ],
        "tap": {"open": "bot"},
    }
    widget.update(over)
    return widget


def _secret_read() -> FieldRead:
    return FieldRead(
        fields={
            "headline": f"deploy key {TOKEN}",
            "state": f"blocked on {TOKEN}",
            "count": f"{TOKEN}",
            "done": 3,
            "rows": [
                {
                    "title": f"rotate {TOKEN} today",
                    "worker_session_key": SESSION_KEY,
                    "crew_key": CREW_KEY,
                },
                {"title": "plain row", "worker_session_key": SESSION_KEY},
                f"bare {TOKEN}",
                {"title": {"nested": TOKEN, TOKEN: SESSION_KEY}},
                {"worker_session_key": SESSION_KEY},
            ],
            "keyed_rows": [{TOKEN: "row keyed by a credential"}, {TOKEN: TOKEN}],
            "events": [
                {"kind": "bind", "text": SESSION_KEY, "ts": "2026-11-18T00:20:00Z"},
                {"kind": "note", "text": f"saw {TOKEN}", "crew_key": CREW_KEY},
            ],
            "series": [{"n": 1, TOKEN: 2}, {"n": 3, "crew_key": CREW_KEY}],
        },
        written_at={"headline": "2026-11-18T00:21:00Z"},
    )


def _snapshot(
    read: FieldRead | None = None, widget: dict[str, Any] | None = None, **kw: Any
) -> dict[str, Any]:
    widget, types = widget or _widget(), _types()
    manifest = ws.cut_manifest(types, widget, slug=SLUG, version=12)
    return ws.shape_snapshot(
        widget,
        types,
        read or _secret_read(),
        manifest,
        bot=kw.pop("bot", BOT),
        layout_version=12,
        now=NOW,
        **kw,
    )


def _wire(snapshot: dict[str, Any]) -> str:
    return json.dumps(snapshot, ensure_ascii=False)


def _assert_clean(snapshot: dict[str, Any]) -> None:
    wire = _wire(snapshot)
    for secret in (TOKEN, SESSION_KEY, CREW_KEY, *MASKED_NAMES):
        assert secret not in wire, f"{secret[:8]}... left through the widget snapshot"


def _block(snapshot: dict[str, Any], label: str) -> dict[str, Any]:
    return next(b for b in snapshot["blocks"] if b.get("label") == label)


# ----------------------------------------------------------------- the acceptance fixture


def test_no_masked_key_session_key_crew_key_or_credential_leaves():
    snap = _snapshot()
    _assert_clean(snap)
    # The values were redacted, not dropped: the blocks still carry their text.
    assert "deploy key" in snap["blocks"][0]["text"]
    assert _block(snap, "State")["text"].startswith("blocked on ")


def test_list_rows_keep_their_redacted_text_and_lose_masked_columns():
    rows = _block(_snapshot(), "Rows")
    texts = [item["text"] for item in rows["items"]]
    assert texts[0].startswith("rotate ") and texts[0].endswith(" today")
    assert "plain row" in texts
    assert any(t.startswith("bare ") for t in texts)
    # The detail column bound to the masked key is empty on every row.
    assert {item["detail"] for item in rows["items"]} == {None}
    # A nested object is no cell text, and a row holding only the masked key has none.
    assert len(rows["items"]) == 3


def test_a_row_key_that_is_a_credential_never_reaches_the_snapshot():
    keyed = _block(_snapshot(), "Keyed")
    assert keyed["items"] == [] and keyed["omitted"] == 0
    _assert_clean(_snapshot())


def test_a_bind_event_keeps_its_line_but_not_its_session_key():
    events = _block(_snapshot(), "Events")
    assert [item["text"] for item in events["items"]][0] == ""
    assert events["items"][1]["text"].startswith("saw ")
    assert {item["detail"] for item in events["items"]} == {None}


def test_a_metric_whose_value_is_a_credential_is_redacted_text():
    metric = _block(_snapshot(), "Count")
    assert metric["value"] == redact_via_context(TOKEN) != TOKEN


# ----------------------------------------------------------------- nearby inputs


def test_a_secret_split_across_label_and_value_is_checked_string_by_string():
    """Each string is redacted on its own, as on the page; the two halves are never
    joined in the body, and either half that is a whole credential is caught."""
    head, tail = TOKEN[:20], TOKEN[20:]
    widget = _widget(
        blocks=[{"type": "status", "label": f"key {head}", "field": "state"}],
    )
    split = _snapshot(FieldRead(fields={"state": tail}), widget)
    assert TOKEN not in _wire(split)
    whole_label = _widget(blocks=[{"type": "status", "label": TOKEN, "field": "state"}])
    _assert_clean(_snapshot(FieldRead(fields={"state": TOKEN}), whole_label))


def test_a_secret_in_a_sparkline_label_is_redacted():
    widget = _widget(
        blocks=[
            {"type": "sparkline", "label": f"per hour {TOKEN}", "field": "series", "point": "n"}
        ]
    )
    snap = _snapshot(FieldRead(fields={"series": [1, 2, 3]}), widget)
    _assert_clean(snap)
    assert snap["blocks"][0]["points"] == [1, 2, 3]


def test_unicode_around_a_secret_does_not_hide_it():
    text = f"\u2192\U0001f511{TOKEN}\u2713 \u65e5\u672c\u8a9e"
    snap = _snapshot(FieldRead(fields={"headline": text, "state": f"\u00e9{TOKEN}\u00e9"}))
    _assert_clean(snap)
    assert snap["blocks"][0]["text"].startswith("\u2192\U0001f511")


def test_a_value_that_is_a_dict_or_a_list_never_emits_its_contents():
    nested = {"inner": {TOKEN: [SESSION_KEY]}, "worker_session_key": SESSION_KEY}
    snap = _snapshot(
        FieldRead(
            fields={
                "headline": [TOKEN, nested],
                "state": nested,
                "count": [TOKEN],
                "done": {"crew_key": CREW_KEY},
                "rows": nested,
                "series": nested,
            }
        )
    )
    _assert_clean(snap)
    assert snap["blocks"][0]["text"] is None
    assert _block(snap, "Count")["value"] is None


def test_envelope_strings_are_redacted():
    widget = _widget(title=f"board {TOKEN}")
    snap = _snapshot(widget=widget, bot={"slug": SLUG, "member": CREW, "name": f"Bot {TOKEN}"})
    _assert_clean(snap)


def test_a_secret_the_sanitizer_joins_is_caught_on_the_way_out():
    """A control character between two halves hides the key from the first pass; the
    sanitizer seam removes it, and the pass over the finished body still catches it."""
    hidden = TOKEN[:12] + "\x07" + TOKEN[12:]
    assert redact_via_context(hidden) == hidden, "the fixture must slip the first pass"
    snap = _snapshot(FieldRead(fields={"headline": hidden}), clean=lambda s: s.replace("\x07", ""))
    _assert_clean(snap)


def test_a_secret_is_redacted_before_the_sanitizer_cuts_it():
    """A length cut after redaction cannot keep most of a credential."""
    widget = _widget(blocks=[{"type": "status", "label": TOKEN + " tail", "field": "state"}])
    cut = len(TOKEN) - 2
    snap = _snapshot(FieldRead(fields={"state": "ok"}), widget, clean=lambda s: s[:cut])
    assert TOKEN[:cut] not in _wire(snap)


def test_a_companion_pattern_applies_at_the_widget_boundary(monkeypatch):
    """Redaction goes through ``redact_via_context``, so a loaded companion's own
    patterns apply here as on the page."""
    marker = "COMPANION-ONLY-SECRET-1234"

    def companion(text: str) -> str:
        return text.replace(marker, "[redacted]")

    monkeypatch.setattr(md, "redact_via_context", companion)
    monkeypatch.setattr(platform_context, "redact_via_context", companion)
    snap = _snapshot(
        FieldRead(fields={"headline": f"x {marker}", "rows": [{"title": marker}]}),
        _widget(title=marker),
    )
    assert marker not in _wire(snap)


# ----------------------------------------------------------------- the resolver


@pytest.mark.asyncio
async def test_the_resolver_redacts_with_the_gateway_state_and_the_bots_slot(monkeypatch):
    state = object()
    seen: list[tuple[Any, str]] = []
    real = md.page_safe_for

    def spy(st: Any, slot: str = "") -> Any:
        seen.append((st, slot))
        return real(None, slot)

    async def in_force(slug: str, member: str) -> tuple[dict[str, Any], int]:
        return {"model": {"types": _types()}, "widgets": [_widget()]}, 12

    monkeypatch.setattr(ws, "package_in_force", in_force)
    monkeypatch.setattr(md, "read_fields", lambda slug, member, manifest: _secret_read())
    monkeypatch.setattr(md, "_dashboard_slot", lambda member, slug: "chat-pebble")
    monkeypatch.setattr(md, "page_safe_for", spy)
    snap = await ws.resolve_snapshot(SLUG, CREW, "secrets", now=NOW, state=state)
    assert snap is not None
    _assert_clean(snap)
    assert (state, "chat-pebble") in seen
