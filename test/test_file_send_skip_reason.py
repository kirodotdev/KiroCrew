"""``file_send`` must report a SKIPPED native channel delivery, not claim success.

The channel endpoint answers "no destination here" as
``{"ok": true, "delivered": false, "skipped": "<reason>"}`` — a closed reason
vocabulary it computes, audits and serializes
(:mod:`kiro_crew.dashboard.upload_destination`). The tool used to read only
``delivered`` and ``error``, so every skip returned a bare ``File sent:`` and the
caller could not tell a delivery from a dashboard-only copy.

These tests pin the reporting contract, not the delivery mechanism: nothing here
changes where a file goes, only what the caller is told about where it went.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_tools.messaging import file_send

#: The reason codes ``upload_destination`` can return, as the tool sees them.
#: Only a missing DESTINATION earns the inline-route remedy.
_REMEDY_REASONS = ("no_channel_destination",)
#: These name no route. ``channel_upload_unsupported`` is here because it spans
#: both file-outbound capabilities -- see ``_describe_channel_skip``'s docstring,
#: which is the single place that roster is written down.
_NO_REMEDY_REASONS = (
    "no_session",
    "restricted_session",
    "channel_upload_unsupported:discord",
    "channel_upload_unsupported:wecom",
)

#: The substring that names the route that DOES work for an inline picture.
_REMEDY_MARKER = "![alt](/abs/path)"


def _describe() -> object:
    """Resolve the helper at CALL time, not import time.

    Deliberate: the end-to-end cases below observe the bug through
    ``file_send``'s own output, and a module-level import of a symbol the fix
    introduces would make this whole file error at COLLECTION when the
    production change is reverted — an import error proves nothing, while a
    failing assertion proves the tests catch the regression.
    """
    from kiro_crew.mcp_tools.messaging import _describe_channel_skip

    return _describe_channel_skip


class TestDescribeChannelSkip:
    """The helper alone — every branch, with no transport in the way."""

    @pytest.mark.parametrize("reason", _REMEDY_REASONS)
    def test_a_missing_destination_names_the_inline_route(self, reason: str) -> None:
        """A session with no eligible upload path is a dead end unless the
        caller is told which route is not a dead end."""
        out = _describe()(reason)
        assert reason in out
        assert _REMEDY_MARKER in out
        assert "working directory" in out

    def test_a_restricted_session_is_never_offered_the_inline_route(self) -> None:
        """The ceiling is not a missing capability.

        ``uploads_restricted`` is the SAME shared predicate the renderer's
        extraction path enforces, so the inline route is equally refused there.
        Naming it would be advice that both fails and reads as a way around a
        privacy boundary — the one branch where a remedy would be wrong.
        """
        out = _describe()("restricted_session")
        assert "restricted_session" in out
        assert _REMEDY_MARKER not in out
        assert "dashboard card is the only delivery" in out

    @pytest.mark.parametrize(
        "reason", ["channel_upload_unsupported:wecom", "channel_upload_unsupported:discord"]
    )
    def test_an_unsupported_channel_is_never_offered_the_inline_route(self, reason: str) -> None:
        """The reason fires for channels on BOTH sides of ``files_outbound``.

        The reason string cannot separate a channel whose renderer would honour
        an inline reference from one that leaves it as literal markup, so naming
        the route would be wrong advice half the time. The per-channel roster
        lives in ``_describe_channel_skip``'s docstring, not here.
        """
        out = _describe()(reason)
        assert reason in out
        assert _REMEDY_MARKER not in out
        assert "no file-upload path" in out

    def test_an_unrecognized_reason_is_reported_verbatim(self) -> None:
        """A reason code added later must degrade to visible, never to silent."""
        out = _describe()("some_future_reason")
        assert "some_future_reason" in out
        assert _REMEDY_MARKER not in out

    @pytest.mark.parametrize("reason", _REMEDY_REASONS + _NO_REMEDY_REASONS)
    def test_every_reason_says_delivery_was_skipped(self, reason: str) -> None:
        """Whatever the branch, the headline fact survives: it did not deliver."""
        assert "skipped" in _describe()(reason)


class TestFileSendSurfacesTheSkip:
    """End to end through the tool, with the endpoint's answer mocked."""

    @pytest.fixture(autouse=True)
    def _isolated_workspace(self, tmp_path, monkeypatch):
        """Pin the workspace under ``tmp_path`` for every case in this class.

        ``file_send`` really copies the file to ``outbox_dir()``, which resolves
        through ``workspace_root()`` and falls back to
        ``Path.home() / "workplace"`` when ``KIROCREW_WORKSPACE`` is unset — no
        conftest fixture isolates that one. Unpinned, every run of this file drops
        an accumulating copy into the operator's real outbox, because
        ``dest.open("xb")`` uniquifies on collision instead of overwriting.
        Autouse rather than per-test so a case added later cannot forget it.
        """
        monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws"))
        self._outbox = tmp_path / "ws" / "outbox"

    def _call(self, tmp_path, channel_response):
        """Run ``file_send`` with the channel endpoint returning *channel_response*."""
        src = tmp_path / "chart.txt"
        src.write_text("hello world", encoding="utf-8")

        def _fake_post(path, *a, **kw):
            if "channel/upload-file" in path:
                return channel_response
            return {"ok": True}

        with (
            patch.object(mcp_core, "_post", side_effect=_fake_post),
            patch.object(
                mcp_core, "require_strict_session_key", return_value=("dashboard:s1", None)
            ),
            patch.object(mcp_core, "_classify_slack_identity", return_value=("unresolved", None)),
        ):
            out = file_send("file_send", {"path": str(src)})
        # The pin is load-bearing, so assert it HELD rather than trusting it: the
        # outbox copy must land under tmp_path, which also proves the write
        # happened somewhere containable at all.
        assert (self._outbox / "chart.txt").exists()
        return out

    @pytest.mark.parametrize("reason", _REMEDY_REASONS)
    def test_the_skip_reason_reaches_the_caller(self, tmp_path, reason: str) -> None:
        """The regression this file exists for: the reason was computed,
        serialized, and then dropped by its only reader."""
        out = self._call(tmp_path, {"ok": True, "delivered": False, "skipped": reason})
        assert reason in out
        assert _REMEDY_MARKER in out

    def test_a_restricted_skip_reports_without_offering_a_bypass(self, tmp_path) -> None:
        out = self._call(
            tmp_path, {"ok": True, "delivered": False, "skipped": "restricted_session"}
        )
        assert "restricted_session" in out
        assert _REMEDY_MARKER not in out

    def test_a_successful_delivery_stays_clean(self, tmp_path) -> None:
        """The success path must not grow skip prose — a delivered file is
        reported exactly as before."""
        out = self._call(tmp_path, {"ok": True, "delivered": True, "channel_type": "telegram"})
        assert "delivered to telegram" in out
        assert "skipped" not in out

    def test_an_error_still_wins_over_a_skip(self, tmp_path) -> None:
        """``error`` and ``skipped`` are different answers. A transport failure
        keeps its own message rather than being reworded as a routing skip."""
        out = self._call(
            tmp_path,
            {"ok": True, "delivered": False, "error": "boom", "skipped": "no_channel_destination"},
        )
        assert "channel upload failed: boom" in out
        assert "native channel delivery skipped" not in out

    def test_a_bare_skipless_response_adds_no_prose(self, tmp_path) -> None:
        """An endpoint answering neither delivered, error nor skipped is not a
        skip to describe — the tool must not invent one."""
        out = self._call(tmp_path, {"ok": True, "delivered": False})
        assert "native channel delivery skipped" not in out


class TestSlackLegSurfacesItsSkipToo:
    """The sibling branch: the Slack endpoint answers skips the same way.

    `/api/slack/upload-file` returns ``{"ok": true, "skipped": "<reason>"}`` from
    `no_slack` and from the shared destination oracle, and ``file_send`` is its
    only caller. Fixing the channel leg and leaving this one would make a point
    patch of a general fix — the same three-state read either way.
    """

    @pytest.fixture(autouse=True)
    def _isolated_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_WORKSPACE", str(tmp_path / "ws"))

    def _call(self, tmp_path, slack_response):
        src = tmp_path / "chart.txt"
        src.write_text("hello world", encoding="utf-8")

        def _fake_post(path, *a, **kw):
            if "slack/upload-file" in path:
                return slack_response
            return {"ok": True}

        with (
            patch.object(mcp_core, "_post", side_effect=_fake_post),
            patch.object(mcp_core, "require_strict_session_key", return_value=(None, None)),
            patch.object(mcp_core, "_classify_slack_identity", return_value=("owner", None)),
        ):
            return file_send("file_send", {"path": str(src)})

    @pytest.mark.parametrize("reason", ["no_slack", "no_channel", "restricted_session"])
    def test_a_slack_skip_is_reported(self, tmp_path, reason: str) -> None:
        out = self._call(tmp_path, {"ok": True, "skipped": reason})
        assert "Slack upload skipped" in out
        assert reason in out

    def test_a_slack_error_still_wins_over_a_skip(self, tmp_path) -> None:
        out = self._call(tmp_path, {"error": "not in channel", "skipped": "no_slack"})
        assert "Slack upload failed: not in channel" in out
        assert "Slack upload skipped" not in out

    def test_a_successful_slack_upload_adds_no_prose(self, tmp_path) -> None:
        out = self._call(tmp_path, {"ok": True})
        assert "Slack upload skipped" not in out
        assert "Slack upload failed" not in out


class TestToolDescriptionMatchesBehaviour:
    def test_the_description_does_not_promise_unconditional_native_delivery(self) -> None:
        """The description is the only thing a fresh caller reads before
        choosing this tool, so it must name the inline route and the fact that
        native delivery can be skipped."""
        from kiro_crew.mcp_tools.messaging import schemas

        spec = next(s for s in schemas() if s["name"] == "file_send")
        desc = spec["description"]
        assert "not guaranteed" in desc
        assert _REMEDY_MARKER in desc
