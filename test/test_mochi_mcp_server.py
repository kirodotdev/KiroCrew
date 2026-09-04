"""Mochi's MCP server — the tools its agents actually call.

The skills are written against these tool names and shapes, so a rename or a
changed return shape silently breaks the autonomous behavior (the agent calls a
tool that no longer exists and gets "Unknown tool" back as prose). These tests
pin the surface and the file round-trips.

The transport itself (JSON-RPC framing, cancellation, worker dispatch) is
``mcp_shared.run_mcp_stdio_loop`` and is covered by its own tests — here we test
the tool layer directly.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.mochi import mcp_server as ms


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the server at a tmp data dir instead of the real app dir."""
    monkeypatch.setattr(ms, "_data_dir", lambda: tmp_path)
    return tmp_path


def _call(name: str, args: dict[str, Any] | None = None) -> str:
    return ms._call_tool(name, args or {})


class TestAuditing:
    """Every tool call is audited, because the gate never sees these calls.

    This server's tools are auto-approved for the app's own agent, so the
    PreToolUse gate — the other place a tool invocation is recorded — is skipped
    entirely. Dispatching straight to the handler therefore left a successful
    mutation (a queue write, a watch item, a pet action) with no trace anywhere.
    """

    def _events(self, monkeypatch):
        captured: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                captured.append(kw)

        monkeypatch.setattr("kiro_crew.mcp_shared.sel", lambda: _Sel())
        return captured

    def test_successful_call_is_logged_as_completed(self, monkeypatch, tmp_path):
        events = self._events(monkeypatch)
        monkeypatch.setattr(ms, "_data_dir", lambda: tmp_path)
        out = ms._call_tool("get_plan", {})
        assert not out.startswith("Error:"), out
        assert len(events) == 1
        assert events[0]["outcome"] == "completed"
        assert events[0]["tool_name"] == "get_plan"
        assert events[0]["source"] == "mcp"

    def test_rejected_arguments_are_logged_as_failed(self, monkeypatch, tmp_path):
        events = self._events(monkeypatch)
        monkeypatch.setattr(ms, "_data_dir", lambda: tmp_path)
        out = ms._call_tool("perform_pet_action", {"action": "not_a_real_action"})
        assert out.startswith("Error:")
        assert len(events) == 1
        assert events[0]["outcome"] == "failed"

    def test_handler_failure_is_logged_as_failed_not_completed(self, monkeypatch, tmp_path):
        """The `Error:` prefix on `_err` is what makes this outcome honest.

        Without it the shared helper's prefix check reads a crashed handler as a
        success, which is worse than no audit line at all.
        """
        events = self._events(monkeypatch)
        monkeypatch.setattr(ms, "_data_dir", lambda: tmp_path)
        monkeypatch.setitem(
            ms._DISPATCH, "get_plan", lambda _a: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        out = ms._call_tool("get_plan", {})
        assert out.startswith("Error:") and "get_plan failed" in out
        assert events[0]["outcome"] == "failed"
    """The skills name these tools; the list is a contract, not an inventory."""

    def test_every_documented_tool_is_exposed(self) -> None:
        names = {t["name"] for t in ms._list_tools()}
        assert names == {
            "get_plan",
            "update_plan",
            "perform_pet_action",
            "get_watchlist",
            "update_watchlist",
            "pin_file",
            "unpin_file",
            "read_mochi_file",
        }

    def test_every_exposed_tool_is_dispatchable(self) -> None:
        """A tool that lists but doesn't dispatch returns prose to the model."""
        for tool in ms._list_tools():
            assert tool["name"] in ms._DISPATCH, f"{tool['name']} lists but has no impl"

    def test_every_tool_has_a_schema_and_description(self) -> None:
        for tool in ms._list_tools():
            assert tool.get("description"), f"{tool['name']} has no description"
            assert tool["inputSchema"]["type"] == "object"

    def test_unknown_tool_is_reported_not_raised(self) -> None:
        assert "Unknown tool" in _call("no_such_tool")


class TestPlan:
    def test_missing_queue_reads_as_empty_plan(self, data_dir: Path) -> None:
        """read_queue returns None for an absent file — must not leak null."""
        out = json.loads(_call("get_plan"))
        assert out["tasks"] == []

    def test_update_then_read_round_trips(self, data_dir: Path) -> None:
        _call("update_plan", {"mood": "curious"})
        out = json.loads(_call("get_plan"))
        assert out.get("mood") == "curious"

    def test_pet_action_is_queued_for_the_runtime(self, data_dir: Path) -> None:
        """Actions are queued, not executed here — the runtime animates."""
        res = json.loads(_call("perform_pet_action", {"action": "notify", "summary": "hi"}))
        assert res["queued"] == "notify"

        plan = json.loads(_call("get_plan"))
        queued = plan["tasks"][-1]
        assert queued["type"] == "notify"
        assert queued["summary"] == "hi"

    def test_query_action_does_not_fabricate_a_position(self, data_dir: Path) -> None:
        """Position lives in the pet window; inventing one would mislead."""
        out = json.loads(_call("perform_pet_action", {"action": "query"}))
        assert "x" not in out and "y" not in out

    def test_concurrent_actions_get_distinct_ids(self, data_dir: Path, monkeypatch) -> None:
        # Same millisecond (frozen clock) foreground + background action: a bare
        # `pa{now}` id would collide and the poller would mark one done and
        # re-run the other forever. The UUID suffix must keep them distinct.
        monkeypatch.setattr(ms, "_now_ms", lambda: 1_700_000_000_000)
        _call("perform_pet_action", {"action": "notify", "summary": "a", "source": "chat"})
        _call("perform_pet_action", {"action": "notify", "summary": "b", "source": "bg"})
        ids = [t["id"] for t in json.loads(_call("get_plan"))["tasks"]]
        assert len(ids) == len(set(ids)), f"duplicate action ids: {ids}"


class TestWatchlist:
    def test_add_then_get_round_trips(self, data_dir: Path) -> None:
        added = json.loads(
            _call(
                "update_watchlist",
                {"add": [{"kind": "url", "label": "docs", "target": "https://example.com"}]},
            )
        )
        assert added["items"] == 1

        out = json.loads(_call("get_watchlist"))
        items = out["watchlist"]["items"]
        assert len(items) == 1
        assert items[0]["label"] == "docs"

    def test_empty_watchlist_reads_clean(self, data_dir: Path) -> None:
        out = json.loads(_call("get_watchlist"))
        assert out["watchlist"]["items"] == []

    def test_archive_only_on_request(self, data_dir: Path) -> None:
        assert "archive" not in json.loads(_call("get_watchlist"))
        assert "archive" in json.loads(_call("get_watchlist", {"includeArchive": True}))

    def test_update_schema_accepts_the_fields_the_shipped_prompts_send(self) -> None:
        # The mochi-watch/poller prompts write historyEntry/notified and
        # mochi-plan writes object notes; with additionalProperties:false the
        # schema MUST list them or kiro-cli rejects every watch-check write,
        # stalling nextCheckAfter and re-spawning the item forever.
        tool = next(t for t in ms._list_tools() if t["name"] == "update_watchlist")
        props = tool["inputSchema"]["properties"]
        upd = props["update"]["items"]["properties"]
        for field in ("historyEntry", "notified", "nextCheckAfter"):
            assert field in upd, f"update schema drops {field} -> spawn storm"
        assert "object" in props["update"]["items"]["properties"]["notes"]["type"]
        assert "object" in props["add"]["items"]["properties"]["notes"]["type"]

    def test_history_entry_write_advances_next_check(self, data_dir: Path) -> None:
        _call(
            "update_watchlist",
            {"add": [{"kind": "url", "label": "d", "target": "x", "checkIntervalMins": 30}]},
        )
        item_id = json.loads(_call("get_watchlist"))["watchlist"]["items"][0]["id"]
        # The exact payload the mochi-watch prompt sends (object notes + history).
        _call(
            "update_watchlist",
            {"update": [{"id": item_id, "historyEntry": {"result": "ok", "changed": False},
                         "notified": True, "notes": {"k": "v"}}]},
        )
        item = json.loads(_call("get_watchlist"))["watchlist"]["items"][0]
        assert item.get("history"), "historyEntry must append history"
        assert item.get("nextCheckAfter"), "nextCheckAfter must advance so it isn't re-checked"


class TestPins:
    def test_pin_then_unpin(self, data_dir: Path) -> None:
        assert json.loads(_call("pin_file", {"path": "/tmp/a.md"}))["pins"] == 1
        assert json.loads(_call("unpin_file", {"path": "/tmp/a.md"}))["removed"] == 1

    def test_pin_is_idempotent(self, data_dir: Path) -> None:
        _call("pin_file", {"path": "/tmp/a.md"})
        again = json.loads(_call("pin_file", {"path": "/tmp/a.md"}))
        assert again.get("alreadyPinned") is True

    def test_pin_requires_a_path(self, data_dir: Path) -> None:
        assert "failed" in _call("pin_file", {})

    def test_rejected_pins_are_error_prefixed_so_the_audit_records_failure(
        self, data_dir: Path
    ) -> None:
        # call_tool_with_logging classifies outcome by an "Error:" prefix; a
        # failure return without it is audited as `completed` — a corrupted SEL
        # record of a security decision. Every rejection path must be prefixed.
        assert _call("pin_file", {}).startswith("Error:")
        assert _call("unpin_file", {}).startswith("Error:")
        # A sensitive path is REJECTED (pinning it would turn the poller into a
        # credential-change side channel) and must likewise read as an error.
        rejected = _call("pin_file", {"path": "~/.ssh/id_rsa"})
        assert rejected.startswith("Error:"), rejected
        assert "not allowed" in rejected

    def test_unpinning_an_unknown_path_is_not_an_error(self, data_dir: Path) -> None:
        assert json.loads(_call("unpin_file", {"path": "/tmp/never.md"}))["removed"] == 0

    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX permission bits only")
    def test_pins_file_is_owner_only(self, data_dir: Path) -> None:
        """Pins record local paths — the file must not be world-readable."""
        _call("pin_file", {"path": "/tmp/a.md"})
        mode = (data_dir / ms._PINNED_FILE).stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0600, got {mode:o}"


class TestReadMochiFile:
    def test_only_known_names_are_readable(self, data_dir: Path) -> None:
        """The agent must not be able to name an arbitrary path."""
        for attempt in ("../../etc/passwd", "/etc/passwd", "nope", ""):
            assert "failed" in _call("read_mochi_file", {"which": attempt})

    def test_absent_file_reports_rather_than_erroring(self, data_dir: Path) -> None:
        out = json.loads(_call("read_mochi_file", {"which": "queue"}))
        assert out["exists"] is False

    def test_returns_contents_when_present(self, data_dir: Path) -> None:
        _call("update_watchlist", {"add": [{"kind": "url", "label": "x", "target": "u"}]})
        raw = _call("read_mochi_file", {"which": "watchlist"})
        assert json.loads(raw)["items"][0]["label"] == "x"


class TestErrorHandling:
    def test_a_failing_tool_returns_text_not_an_exception(
        self, monkeypatch: pytest.MonkeyPatch, data_dir: Path
    ) -> None:
        """A raised exception becomes a protocol error the model can't act on."""

        def _boom(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(ms.qf, "read_queue", _boom)
        out = _call("get_plan")
        assert "get_plan failed" in out
        assert "disk on fire" in out


class TestArgumentValidation:
    """Arguments are validated against the declared inputSchema BEFORE dispatch.

    The handlers persist their arguments (queue / watchlist files the poller
    executes every cycle), so an unvalidated malformed write would wedge every
    subsequent poll — the rejection has to happen at the protocol boundary.
    """

    def test_wrong_type_is_rejected_before_persistence(self, data_dir: Path) -> None:
        out = _call("update_plan", {"tasks_to_add": "x"})
        assert "update_plan failed" in out
        assert "expected array" in out
        # Nothing was persisted: the queue read back has no wedged task.
        raw = _call("read_mochi_file", {"which": "queue"})
        assert '"x"' not in raw

    def test_incomplete_full_replace_is_rejected_before_persistence(self, data_dir: Path) -> None:
        """A full_replace missing timestamps would overwrite the queue with a
        shape read_queue() rejects — losing the plan. It must fail at validation."""
        out = _call("update_plan", {"full_replace": {"tasks": []}})
        assert "update_plan failed" in out
        assert "required" in out

    def test_complete_full_replace_passes(self, data_dir: Path) -> None:
        """The documented full_replace shape (timestamps + tasks) still succeeds."""
        out = _call(
            "update_plan",
            {
                "full_replace": {
                    "planned_at": "2026-08-02T20:00:00Z",
                    "planned_until": "2026-08-02T21:00:00Z",
                    "tasks": [],
                }
            },
        )
        assert "update_plan failed" not in out

    def test_bad_enum_is_rejected(self, data_dir: Path) -> None:
        out = _call("perform_pet_action", {"action": "explode"})
        assert "perform_pet_action failed" in out
        assert "enum" in out

    def test_unknown_key_is_rejected(self, data_dir: Path) -> None:
        out = _call("perform_pet_action", {"action": "notify", "summary": "x", "bogus": 1})
        assert "unknown field" in out

    def test_missing_required_is_rejected(self, data_dir: Path) -> None:
        out = _call("pin_file", {})
        assert "required" in out

    def test_every_documented_notify_field_still_passes(self, data_dir: Path) -> None:
        """The fail-closed check must not reject the calls the prompts teach."""
        out = _call(
            "perform_pet_action",
            {
                "action": "notify",
                "summary": "hi",
                "chatMessage": "detail",
                "mood": "happy",
                "sticky": True,
                "pushToChat": True,
                "priority": "critical",
                "source": "reminder",
                "watchItemId": "w1",
            },
        )
        assert '"ok": true' in out or '"ok":true' in out.replace(" ", "")

    def test_schema_map_covers_every_dispatched_tool(self) -> None:
        """A tool added to _DISPATCH without a schema would silently accept only
        {} (fail-closed) — surface the mismatch here instead of in production."""
        assert set(ms._DISPATCH) == set(ms._INPUT_SCHEMAS)


class TestQueueMutationLock:
    """The queue file has two writers in two processes (this MCP server and the
    gateway's poller). ``write_queue_atomic`` makes each write atomic, but a
    read-modify-write is not — ``queue_mutation`` is the cross-process lock
    that closes the lost-update window. flock contends between separate file
    descriptors even within one process, so threads exercise the real lock.
    """

    def test_concurrent_appends_are_not_lost(self, data_dir: Path) -> None:
        import threading

        from kiro_crew.apps.builtins.mochi import queue_file as qf

        path = str(data_dir / "mochi-queue.json")
        now = ms._now_ms() if hasattr(ms, "_now_ms") else 0

        def append(n: int) -> None:
            with qf.queue_mutation(path):
                queue = qf.read_queue(path) or {
                    "planned_at": qf._iso(now),
                    "planned_until": qf._iso(now + 60_000),
                    "tasks": [],
                }
                queue["tasks"] = list(queue.get("tasks", [])) + [
                    {"id": f"t{n}", "type": "notify", "done": False}
                ]
                qf.write_queue_atomic(path, queue)

        threads = [threading.Thread(target=append, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = qf.read_queue(path)
        assert final is not None
        # Without the lock this loses appends nondeterministically; with it,
        # all 16 survive.
        assert len(final["tasks"]) == 16

    def test_lock_file_is_a_sibling_not_the_data_file(self, data_dir: Path) -> None:
        """The lock fd must not be the file being atomically REPLACED — flock
        follows the inode, and the rename swaps inodes out from under it."""
        from kiro_crew.apps.builtins.mochi import queue_file as qf

        path = data_dir / "mochi-queue.json"
        with qf.queue_mutation(path):
            pass
        assert (data_dir / "mochi-queue.json.lock").exists()
        assert not path.exists()  # taking the lock must not create the data file
