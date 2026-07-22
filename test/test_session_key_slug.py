"""slot keys must be ASCII so the derived session key is header-safe.

A dashboard session key is ``dashboard:{slot.key}`` and is sent to the gateway
as the ``X-Session-Key`` request header. HTTP header values are latin-1
(RFC 7230), so a slot key containing a character outside latin-1 (e.g. an
em-dash U+2014 from a title-derived slot name) would make ``http.client`` raise
``UnicodeEncodeError`` and abort the tool call.

The fix is at the source: ``get_or_create_slot`` slugs any non-ASCII slot name
to ASCII *before* the lookup/create, so the stored slot key — and therefore the
injected session key — is always header-safe, and create + repeat calls with the
same raw name converge on the one slot. (The transport-layer guard that rejects a
genuinely non-latin-1 session key is ``mcp_core._session_key_header_error``,
landed under and covered by ``test_mcp_core.py``.)
"""

from __future__ import annotations

from chat_test_helpers import _make_state

from kiro_crew.dashboard.state import _ascii_slot_key, _normalize_slot_key
from kiro_crew.history import _safe_key

EM_DASH = "\u2014"


class TestAsciiSlotKey:
    def test_ascii_unchanged(self):
        assert _ascii_slot_key("plain-ascii_1.2") == "plain-ascii_1.2"

    def test_non_ascii_replaced(self):
        assert _ascii_slot_key(f"a{EM_DASH}b") == "a-b"

    def test_control_chars_replaced(self):
        # CR/LF are ASCII, so they slip past an isascii() check, but in a slot
        # key they flow into the X-Session-Key header and enable header
        # injection/splitting. They must be replaced (hardening).
        out = _ascii_slot_key("a\r\nX-Evil: 1")
        assert "\r" not in out and "\n" not in out
        assert out == "a--X-Evil: 1"

    def test_idempotent(self):
        once = _ascii_slot_key(f"Plan {EM_DASH} v2")
        assert _ascii_slot_key(once) == once
        once.encode("latin-1")  # the slugged key is header-safe


class TestGetOrCreateSlotSlugging:
    def test_non_ascii_name_is_slugged(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(f"Plan {EM_DASH} v2")
        assert slot.key.isascii()
        assert EM_DASH not in slot.key
        # the resulting session key is header-safe
        f"dashboard:{slot.key}".encode("latin-1")

    def test_repeat_call_resolves_same_slot(self, tmp_path):
        """Slugging runs before the lookup, so the same name matches the slot."""
        state = _make_state(tmp_path)
        first = state.get_or_create_slot(f"Plan {EM_DASH} v2")
        again = state.get_or_create_slot(f"Plan {EM_DASH} v2")
        assert again is first

    def test_ascii_name_unchanged(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("my-session")
        assert slot.key == "my-session"

    def test_auto_generated_key_is_ascii(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot()
        assert slot.key.isascii()
        assert slot.key.startswith("chat-")


class TestNormalizeSlotKey:
    """Slot keys must equal their persisted JSONL filename stem.

    ``history._safe_key()`` folds ``[^\\w\\-.]`` to ``_`` when building the
    session filename; on restart ``restore_recent_sessions`` derives slot names
    from filename stems. If the live key diverges from that fold (e.g.
    ``Artifact: My Doc``), the two restore paths create duplicate sidebar
    sessions backed by one transcript.
    """

    def test_display_name_folds_to_filename_charset(self):
        assert (
            _normalize_slot_key("Artifact: 2026 Code Activity Benchmark - nrb vs Dan Lloyd Org")
            == "Artifact__2026_Code_Activity_Benchmark_-_nrb_vs_Dan_Lloyd_Org"
        )

    def test_composes_ascii_fold_then_filename_fold(self):
        # em-dash → "-" (ASCII fold), space → "_" (filename fold)
        assert _normalize_slot_key(f"Plan {EM_DASH} v2") == "Plan_-_v2"

    def test_safe_chars_unchanged(self):
        assert _normalize_slot_key("chat-3-1781923451") == "chat-3-1781923451"
        assert _normalize_slot_key("plain-ascii_1.2") == "plain-ascii_1.2"

    def test_idempotent(self):
        once = _normalize_slot_key("Artifact: My Doc")
        assert _normalize_slot_key(once) == once

    def test_transport_prefix_stripped(self):
        """Full session keys / filename stems fold to the bare canonical key,
        mirroring _history_key_for so both name forms share one slot."""
        assert _normalize_slot_key("dashboard:chat-1") == "chat-1"
        assert _normalize_slot_key("dashboard_chat-1") == "chat-1"
        assert _normalize_slot_key("dashboard:Artifact: My Doc") == "Artifact__My_Doc"

    def test_round_trips_through_history_filename(self):
        """The invariant the fix rests on: key == filename stem after _safe_key."""
        key = _normalize_slot_key("Artifact: My Doc (v2)")
        assert _safe_key(f"dashboard:{key}") == f"dashboard_{key}"


class TestGetOrCreateSlotFilenameNormalization:
    def test_display_name_creates_folded_key(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("Artifact: My Doc")
        assert slot.key == "Artifact__My_Doc"

    def test_pretty_name_preserved_as_title(self, tmp_path):
        """The human-readable requested name seeds the title (non-pinned)."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("Artifact: My Doc")
        assert slot.title == "Artifact: My Doc"
        assert slot._titled is False  # auto-title may still improve it

    def test_unchanged_name_gets_no_title_seed(self, tmp_path):
        """Filename-safe names keep title==key (display shows placeholder)."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-9-123")
        assert slot.title == slot.key

    def test_raw_and_folded_names_resolve_to_same_slot(self, tmp_path):
        """Both key forms converge — the source of the duplicate-session bug."""
        state = _make_state(tmp_path)
        first = state.get_or_create_slot("Artifact: My Doc")
        again_raw = state.get_or_create_slot("Artifact: My Doc")
        again_folded = state.get_or_create_slot("Artifact__My_Doc")
        assert again_raw is first
        assert again_folded is first
