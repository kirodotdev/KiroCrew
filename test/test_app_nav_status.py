"""Tests for the app nav-status hook (issue #520).

An app reports a coarse runtime status via AppContext.set_nav_status(tone, label);
the core broadcasts an ``app_nav_status`` frame that the dashboard renders on the
app's sidebar icon. Tests observe the PUBLIC hook via the broadcast payload — the
app's own state names are never constrained; only render safety (tone membership +
label sanitization) is enforced.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.apps.context import build_app_context, read_persisted_nav_status

_EVENTS_PERM = {"events": ["app_nav_status"], "storage": True}


def _ctx_with_capture(tmp_path: Path):
    published: list[dict] = []
    ctx = build_app_context(
        app_name="midway-status",
        data_dir=tmp_path,
        permissions=_EVENTS_PERM,
        broadcast_fn=lambda payload: published.append(payload),
    )
    return ctx, published


class TestSetNavStatus:
    def test_broadcasts_app_nav_status_frame(self, tmp_path: Path) -> None:
        """A valid tone + label broadcasts one app_nav_status frame for this app."""
        ctx, published = _ctx_with_capture(tmp_path)

        ctx.set_nav_status("caution", "Expiring 12m")

        assert len(published) == 1
        frame = published[0]
        assert frame["type"] == "app_nav_status"
        assert frame["app"] == "midway-status"
        assert frame["data"]["tone"] == "caution"
        assert frame["data"]["label"] == "Expiring 12m"

    def test_unknown_tone_degrades_to_neutral(self, tmp_path: Path) -> None:
        """An app's typo/unknown tone renders as neutral rather than raising."""
        ctx, published = _ctx_with_capture(tmp_path)

        ctx.set_nav_status("bogus-tone", "hi")

        assert published[0]["data"]["tone"] == "neutral"

    def test_label_is_length_capped(self, tmp_path: Path) -> None:
        """An oversized label is capped so an app cannot flood the nav chrome."""
        ctx, published = _ctx_with_capture(tmp_path)

        ctx.set_nav_status("positive", "x" * 500)

        assert len(published[0]["data"]["label"]) <= 48

    def test_persists_last_status_to_storage(self, tmp_path: Path) -> None:
        """The last status is persisted under the reserved key for fresh-load."""
        ctx, _ = _ctx_with_capture(tmp_path)

        ctx.set_nav_status("positive", "Valid")

        assert ctx.storage is not None
        assert ctx.storage.get("_nav_status") == {"tone": "positive", "label": "Valid"}

    def test_noop_without_events_permission(self, tmp_path: Path) -> None:
        """An app that declared no events cannot push nav status; hook is a no-op."""
        published: list[dict] = []
        ctx = build_app_context(
            app_name="quiet-app",
            data_dir=tmp_path,
            permissions={"storage": True},
            broadcast_fn=lambda payload: published.append(payload),
        )

        assert ctx.events is None
        ctx.set_nav_status("running", "x")  # must not raise

        assert published == []

    def test_noop_when_nav_event_not_permitted(self, tmp_path: Path) -> None:
        """An app that declared other events but not app_nav_status no-ops (no raise)."""
        published: list[dict] = []
        ctx = build_app_context(
            app_name="other-app",
            data_dir=tmp_path,
            permissions={"events": ["something_else"], "storage": True},
            broadcast_fn=lambda payload: published.append(payload),
        )

        ctx.set_nav_status("critical", "boom")  # must not raise PermissionError

        assert published == []


class TestReadPersistedNavStatus:
    def test_roundtrip(self, tmp_path: Path) -> None:
        """The reader returns the last status persisted by set_nav_status."""
        ctx, _ = _ctx_with_capture(tmp_path)
        ctx.set_nav_status("caution", "Expiring")

        assert read_persisted_nav_status(tmp_path) == {"tone": "caution", "label": "Expiring"}

    def test_absent_returns_none(self, tmp_path: Path) -> None:
        """No persisted status yields None (fresh app shows no indicator)."""
        assert read_persisted_nav_status(tmp_path) is None

    def test_corrupt_tone_coerced_on_read(self, tmp_path: Path) -> None:
        """A persisted-but-unknown tone is coerced to neutral on read (defensive)."""
        from kiro_crew.apps.app_storage import AppStorage

        AppStorage("x-app", tmp_path).set("_nav_status", {"tone": "hacked", "label": "x"})

        assert read_persisted_nav_status(tmp_path) == {"tone": "neutral", "label": "x"}
