"""Tests for heartbeat task retention via HEARTBEAT_KEEP sentinel."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew.heartbeat as hb_mod
from kiro_crew.heartbeat import (
    _HEADER,
    HeartbeatService,
    _should_keep,
    is_keep_response,
    strip_keep_sentinel,
)


class TestShouldKeep:
    def test_none_returns_false(self) -> None:
        assert not _should_keep(None)

    def test_empty_string_returns_false(self) -> None:
        assert not _should_keep("")

    def test_normal_response_returns_false(self) -> None:
        assert not _should_keep("Ticket is resolved. All done!")

    def test_sentinel_returns_true(self) -> None:
        assert _should_keep("Ticket still open. HEARTBEAT_KEEP")

    def test_sentinel_case_insensitive(self) -> None:
        assert _should_keep("Not done yet. heartbeat_keep")

    def test_sentinel_mid_text(self) -> None:
        assert _should_keep("Status: Assigned. HEARTBEAT_KEEP — will retry.")


class TestStripKeepSentinel:
    def test_removes_sentinel(self) -> None:
        assert strip_keep_sentinel("Still open. HEARTBEAT_KEEP") == "Still open."

    def test_no_sentinel_unchanged(self) -> None:
        assert strip_keep_sentinel("All done!") == "All done!"

    def test_empty_string(self) -> None:
        assert strip_keep_sentinel("") == ""


class TestHeartbeatRetention:
    @pytest.mark.asyncio
    async def test_completed_task_removed(self, tmp_path: Path) -> None:
        """Task without HEARTBEAT_KEEP is removed after processing."""

        async def on_task(text: str, deliver: str) -> str:
            return "Done! Ticket resolved."

        svc = HeartbeatService(memory=MagicMock(), on_task=on_task)
        hb_path = tmp_path / "HEARTBEAT.md"
        hb_path.write_text(_HEADER + "- Check ticket ABC\n")

        original = hb_mod.heartbeat_path
        hb_mod.heartbeat_path = lambda: hb_path
        try:
            await svc._process_heartbeat_file()
        finally:
            hb_mod.heartbeat_path = original

        content = hb_path.read_text()
        assert "Check ticket ABC" not in content

    @pytest.mark.asyncio
    async def test_incomplete_task_kept(self, tmp_path: Path) -> None:
        """Task with HEARTBEAT_KEEP in response is retained."""

        async def on_task(text: str, deliver: str) -> str:
            return "Ticket still Assigned. HEARTBEAT_KEEP"

        svc = HeartbeatService(memory=MagicMock(), on_task=on_task)
        hb_path = tmp_path / "HEARTBEAT.md"
        hb_path.write_text(_HEADER + "- Check ticket ABC\n")

        original = hb_mod.heartbeat_path
        hb_mod.heartbeat_path = lambda: hb_path
        try:
            await svc._process_heartbeat_file()
        finally:
            hb_mod.heartbeat_path = original

        content = hb_path.read_text()
        assert "Check ticket ABC" in content

    @pytest.mark.asyncio
    async def test_mixed_tasks_partial_retention(self, tmp_path: Path) -> None:
        """Only incomplete tasks are retained; completed ones are removed."""

        async def on_task(text: str, deliver: str) -> str:
            if "pending" in text:
                return "Still pending. HEARTBEAT_KEEP"
            return "All done!"

        svc = HeartbeatService(memory=MagicMock(), on_task=on_task)
        hb_path = tmp_path / "HEARTBEAT.md"
        hb_path.write_text(_HEADER + "- Check pending ticket\n- Check resolved ticket\n")

        original = hb_mod.heartbeat_path
        hb_mod.heartbeat_path = lambda: hb_path
        try:
            await svc._process_heartbeat_file()
        finally:
            hb_mod.heartbeat_path = original

        content = hb_path.read_text()
        assert "pending ticket" in content
        assert "resolved ticket" not in content

    @pytest.mark.asyncio
    async def test_exception_still_retains(self, tmp_path: Path) -> None:
        """Tasks that raise exceptions are still retained (existing behavior)."""

        async def on_task(text: str, deliver: str) -> str:
            raise RuntimeError("connection error")

        svc = HeartbeatService(memory=MagicMock(), on_task=on_task)
        hb_path = tmp_path / "HEARTBEAT.md"
        hb_path.write_text(_HEADER + "- Check ticket XYZ\n")

        original = hb_mod.heartbeat_path
        hb_mod.heartbeat_path = lambda: hb_path
        try:
            await svc._process_heartbeat_file()
        finally:
            hb_mod.heartbeat_path = original

        content = hb_path.read_text()
        assert "Check ticket XYZ" in content

    @pytest.mark.asyncio
    async def test_none_return_treated_as_complete(self, tmp_path: Path) -> None:
        """Callback returning None (legacy behavior) removes the task."""

        async def on_task(text: str, deliver: str) -> None:
            pass

        svc = HeartbeatService(memory=MagicMock(), on_task=on_task)
        hb_path = tmp_path / "HEARTBEAT.md"
        hb_path.write_text(_HEADER + "- Legacy task\n")

        original = hb_mod.heartbeat_path
        hb_mod.heartbeat_path = lambda: hb_path
        try:
            await svc._process_heartbeat_file()
        finally:
            hb_mod.heartbeat_path = original

        content = hb_path.read_text()
        assert "Legacy task" not in content

    @pytest.mark.asyncio
    async def test_deliver_tag_preserved_on_keep(self, tmp_path: Path) -> None:
        """Deliver target is preserved when task is retained."""

        async def on_task(text: str, deliver: str) -> str:
            return "Not done. HEARTBEAT_KEEP"

        svc = HeartbeatService(memory=MagicMock(), on_task=on_task)
        hb_path = tmp_path / "HEARTBEAT.md"
        hb_path.write_text(
            _HEADER + "- Check ticket  <!-- deliver:C08HZAWV4TP -->\n"
        )

        original = hb_mod.heartbeat_path
        hb_mod.heartbeat_path = lambda: hb_path
        try:
            await svc._process_heartbeat_file()
        finally:
            hb_mod.heartbeat_path = original

        content = hb_path.read_text()
        assert "Check ticket" in content
        assert "deliver:C08HZAWV4TP" in content


class TestDeliverySuppression:
    """Tests for the gateway-level delivery suppression logic.

    The gateway suppresses _deliver_result when is_keep_response() returns True.
    """

    def test_incomplete_task_suppresses_delivery(self) -> None:
        """When HEARTBEAT_KEEP is present → suppress."""
        assert is_keep_response("Ticket still Assigned. HEARTBEAT_KEEP")

    def test_completed_task_allows_delivery(self) -> None:
        """When no HEARTBEAT_KEEP → deliver."""
        assert not is_keep_response("Ticket resolved! All done.")

    def test_no_response_allows_delivery(self) -> None:
        """_No response._ (fallback) has no sentinel → deliver."""
        assert not is_keep_response("_No response._")

    def test_none_allows_delivery(self) -> None:
        """None response → deliver."""
        assert not is_keep_response(None)

    def test_case_insensitive(self) -> None:
        """Lowercase variant also suppresses."""
        assert is_keep_response("Still checking. heartbeat_keep")
