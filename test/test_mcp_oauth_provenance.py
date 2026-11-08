"""An MCP sign-in banner read back from a transcript shows its link only when
this gateway's own ACP child sent that exact URL.

A transcript line is agent-writable, and the consent-URL gate exempts the
protocol's own high-entropy parameters at trusted endpoints. These tests pin that
a forged ``mcp_oauth`` row, even one copying a live child's stamp, cannot use the
exemption to draw a secret as a real Authorize link, while a banner the child
really emitted keeps its link on every read.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard import mcp_oauth_provenance
from kiro_crew.dashboard.chat_runner import _emit_mcp_oauth_request, _mark_mcp_oauth_completed
from kiro_crew.dashboard.chat_utils import _prepare_messages, _redact_meta_for_role
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.dashboard.state_parts.transcript_rows import chat_message_note
from kiro_crew.security import oauth_url_contains_credential

LIVE_CHILD = "c0ffee0123456789"
STATE = "opaque-state-123"
CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
CLIENT_ID = "client123"
# Shaped like an AWS secret access key but free of `+` and `/`, so it reads as
# ordinary base64url protocol entropy at an approved endpoint.
SECRET = "wJalrXUtnFEMIxK7MDEN" "GybPxRfiCYEXAMPLEKEY"
NOTION_URL = (
    "https://api.notion.com/v1/oauth/authorize"
    f"?client_id={CLIENT_ID}&response_type=code"
    f"&state={STATE}&code_challenge={CHALLENGE}"
    "&code_challenge_method=S256"
)


def _forged_url(parameter: str) -> str:
    original = {"state": STATE, "client_id": CLIENT_ID, "code_challenge": CHALLENGE}[parameter]
    value = SECRET + "abc" if parameter == "code_challenge" else SECRET
    return NOTION_URL.replace(f"{parameter}={original}", f"{parameter}={value}", 1)


def _row(url: str, child: str = LIVE_CHILD, **extra) -> dict:
    meta = {"server_name": "notion", "oauth_url": url, "child": child, **extra}
    return {"role": "mcp_oauth", "content": "🔐 notion requires authentication.", "meta": meta}


def _slot() -> _ChatSlot:
    return _ChatSlot("s1")


def _state() -> MagicMock:
    return MagicMock()


class TestForgedRowIsNotALink:
    def test_secret_in_state_is_exempt_from_the_shape_gate(self):
        # The reason provenance is needed: at this approved endpoint the shape
        # gate alone lets the secret through.
        assert oauth_url_contains_credential(_forged_url("state")) is False

    @pytest.mark.parametrize("parameter", ["state", "client_id", "code_challenge"])
    def test_forged_row_with_live_child_stamp_renders_expired_without_a_link(self, parameter):
        url = _forged_url(parameter)
        [out] = _prepare_messages([_row(url)], False, live_child=LIVE_CHILD)
        assert SECRET not in str(out["meta"])
        assert not out["meta"].get("oauth_url")
        assert out["meta"]["expired"] is True

    @pytest.mark.parametrize("parameter", ["state", "client_id"])
    def test_read_back_on_resume_drops_the_forged_link(self, parameter):
        # Resume and rehydrate run `_redact_meta_for_role` on each restored row
        # and may broadcast the result straight to open tabs, outside the
        # liveness gate, so the provenance check must hold there on its own.
        out = _redact_meta_for_role("mcp_oauth", _row(_forged_url(parameter))["meta"])
        assert "oauth_url" not in out
        assert out["expired"] is True
        assert out["server_name"] == "notion"

    def test_forged_row_copying_a_genuine_stamp_and_another_url_is_withheld(self):
        _emit_mcp_oauth_request(_state(), _slot(), "notion", NOTION_URL, minted_by=LIVE_CHILD)
        [out] = _prepare_messages([_row(_forged_url("state"))], False, live_child=LIVE_CHILD)
        assert not out["meta"].get("oauth_url")
        assert out["meta"]["expired"] is True

    def test_genuine_url_under_another_child_stamp_is_withheld(self):
        _emit_mcp_oauth_request(_state(), _slot(), "notion", NOTION_URL, minted_by="other-child")
        [out] = _prepare_messages([_row(NOTION_URL)], False, live_child=LIVE_CHILD)
        assert not out["meta"].get("oauth_url")

    def test_terminal_row_loses_the_url_without_turning_expired(self):
        meta = _row(_forged_url("state"), completed=True)["meta"]
        out = _redact_meta_for_role("mcp_oauth", meta)
        assert "oauth_url" not in out
        assert "expired" not in out
        assert out["completed"] is True

    @pytest.mark.parametrize(
        "meta",
        [
            {"child": "bad\ud800child", "oauth_url": NOTION_URL},
            {"child": LIVE_CHILD, "oauth_url": NOTION_URL + "\udfff"},
        ],
    )
    def test_lone_surrogate_from_a_transcript_line_is_withheld_not_raised(self, meta):
        # JSON decodes "\ud800" to a lone surrogate, which strict UTF-8 cannot
        # encode; the read must still answer, or the whole chat fails to open.
        row = {"role": "mcp_oauth", "content": "x", "meta": {"server_name": "notion", **meta}}
        [out] = _prepare_messages([row], False, live_child=LIVE_CHILD)
        assert not out["meta"].get("oauth_url")
        assert out["meta"]["expired"] is True

    @pytest.mark.parametrize("child", [None, "", 7, ["x"]])
    def test_malformed_child_stamp_is_never_vouched_for(self, child):
        mcp_oauth_provenance.record_emitted(LIVE_CHILD, NOTION_URL)
        meta = {"server_name": "notion", "oauth_url": NOTION_URL, "child": child}
        assert "oauth_url" not in _redact_meta_for_role("mcp_oauth", meta)


class TestLiveFrame:
    """The ``chat_message`` frame a re-appended row is broadcast in.

    ``refresh_channel_window`` re-appends a channel transcript's tail with its
    stored meta and cls and broadcasts it, so a forged line reaches open tabs
    through this builder without any HTTP read.
    """

    def test_forged_row_meta_leaves_without_its_link(self):
        msg = _row(_forged_url("state"))
        frame = chat_message_note("s1", msg, workspace=None)
        assert SECRET not in json.dumps(frame)
        assert not frame["meta"].get("oauth_url")
        assert frame["meta"]["expired"] is True

    def test_forged_row_cls_leaves_without_its_link(self):
        # A JSON `cls` is lifted into the frame's meta too, so it is gated the same.
        forged_meta = _row(_forged_url("client_id"))["meta"]
        msg = {"role": "mcp_oauth", "content": "x", "cls": json.dumps(forged_meta)}
        frame = chat_message_note("s1", msg, workspace=None)
        assert not frame.get("meta", {}).get("oauth_url")

    def test_emitted_banner_frame_keeps_its_link(self):
        slot = _slot()
        _emit_mcp_oauth_request(_state(), slot, "notion", NOTION_URL, minted_by=LIVE_CHILD)
        [row] = [m for m in slot.messages if m.get("role") == "mcp_oauth"]
        frame = chat_message_note("s1", row, workspace=None)
        assert frame["meta"]["oauth_url"] == NOTION_URL
        assert "expired" not in frame["meta"]


class TestGenuineBannerKeepsItsLink:
    def test_emitted_banner_renders_its_link_on_read_back(self):
        slot = _slot()
        _emit_mcp_oauth_request(_state(), slot, "notion", NOTION_URL, minted_by=LIVE_CHILD)
        [row] = [m for m in slot.messages if m.get("role") == "mcp_oauth"]
        [out] = _prepare_messages([row], False, live_child=LIVE_CHILD)
        assert out["meta"]["oauth_url"] == NOTION_URL
        assert "expired" not in out["meta"]

    def test_save_path_keeps_the_emitted_url(self):
        slot = _slot()
        _emit_mcp_oauth_request(_state(), slot, "notion", NOTION_URL, minted_by=LIVE_CHILD)
        [row] = [m for m in slot.messages if m.get("role") == "mcp_oauth"]
        assert _redact_meta_for_role("mcp_oauth", row["meta"])["oauth_url"] == NOTION_URL

    def test_completion_of_an_emitted_banner_is_unchanged(self):
        slot = _slot()
        _emit_mcp_oauth_request(_state(), slot, "notion", NOTION_URL, minted_by=LIVE_CHILD)
        _mark_mcp_oauth_completed(_state(), slot, "notion", success=True)
        [row] = [m for m in slot.messages if m.get("role") == "mcp_oauth"]
        assert row["meta"]["completed"] is True
        assert "expired" not in row["meta"]


class TestRecord:
    def test_restart_forgets_every_link_the_old_children_sent(self):
        # A restart replaces every child, so no stored link is redeemable any
        # more; the empty record agrees with the liveness gate.
        mcp_oauth_provenance.record_emitted(LIVE_CHILD, NOTION_URL)
        mcp_oauth_provenance._emitted.clear()
        assert mcp_oauth_provenance.was_emitted(LIVE_CHILD, NOTION_URL) is False

    def test_empty_child_or_url_is_not_recorded(self):
        mcp_oauth_provenance.record_emitted("", NOTION_URL)
        mcp_oauth_provenance.record_emitted(LIVE_CHILD, "")
        assert len(mcp_oauth_provenance._emitted) == 0

    def test_record_is_bounded_and_keeps_the_recently_read(self, monkeypatch):
        monkeypatch.setattr(mcp_oauth_provenance, "CAP", 3)
        for i in range(3):
            mcp_oauth_provenance.record_emitted(LIVE_CHILD, f"{NOTION_URL}&n={i}")
        assert mcp_oauth_provenance.was_emitted(LIVE_CHILD, f"{NOTION_URL}&n=0")
        mcp_oauth_provenance.record_emitted(LIVE_CHILD, f"{NOTION_URL}&n=3")
        assert len(mcp_oauth_provenance._emitted) == 3
        assert mcp_oauth_provenance.was_emitted(LIVE_CHILD, f"{NOTION_URL}&n=0")
        assert not mcp_oauth_provenance.was_emitted(LIVE_CHILD, f"{NOTION_URL}&n=1")

    def test_only_digests_are_kept(self):
        mcp_oauth_provenance.record_emitted(LIVE_CHILD, NOTION_URL)
        [key] = mcp_oauth_provenance._emitted
        assert isinstance(key, bytes) and len(key) == 32
