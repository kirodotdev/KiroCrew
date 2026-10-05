"""``spawn_run`` tells the caller when a spawn WAITS instead of claiming it started.

The gateway defers a spawn the memory guard (or a paused adaptive cap) will not
admit yet: the row stays queued and is re-checked every admit wait, possibly for
hours on a host that never clears the floor. ``POST /api/spawn`` answers such a
row with ``status: "queued"`` plus the gate's own reason, and the tool relays it
as ``Queued …`` -- the agent that read ``Spawned 1 subagent(s)`` waited for a
completion event that was never coming.

An answer without ``status`` (an older gateway) or with ``status: "spawned"`` (a
row waiting only for a stagger tick behind the cap) keeps the existing text.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from kiro_crew.mcp_tools import spawn as spawn_tools

_DETAIL = "low memory: 2.2 GB available, need 2.5 GB (0.50 GB for this start)"


def _run(answers: list[dict]) -> str:
    calls = iter(answers)

    def _post(path: str, body: dict) -> dict:
        if path == "/api/spawn":
            return next(calls)
        return {}

    with (
        patch.object(spawn_tools.mcp_core, "_post", side_effect=_post),
        patch.object(spawn_tools.mcp_core, "_resolve_session_key", return_value="chat-1"),
    ):
        return spawn_tools.spawn_run(
            "spawn_run",
            {
                "tasks": [f"task {i}" for i in range(len(answers))],
                # A one-task call is roster-checked before it is posted; the
                # reason lets it through so the gateway's answer is what is tested.
                "solo_reason": "bulk_data",
                "solo_details": "a large log only the summary of which is needed",
            },
        )


class TestDeferredSpawnIsReportedAsQueued:
    def test_a_deferred_spawn_says_queued_and_why(self) -> None:
        out = _run(
            [{"id": "q1", "status": "queued", "reason": "low_memory", "reason_detail": _DETAIL}]
        )
        first = out.splitlines()[0]
        # Same ``N subagent(s).`` marker as the Spawned header, so the
        # dashboard run card still recognises the launch and reads the id
        # lines below it (a queued-only wave otherwise rendered no card).
        assert first.startswith("Queued 1 subagent(s). Not started yet: ")
        assert _DETAIL in first
        assert "  q1: task 0" in out.splitlines()[1]
        assert "Spawned" not in out
        # The id still travels: the run card and the wave reconcile key on it.
        assert "q1" in out
        # A queued spawn is not a failure.
        assert not out.startswith("Error")
        assert "failed to start" not in out

    def test_a_mixed_wave_reports_both_groups(self) -> None:
        out = _run(
            [
                {"id": "s1"},
                {"id": "q1", "status": "queued", "reason": "low_memory", "reason_detail": _DETAIL},
            ]
        )
        assert "Spawned 1 subagent(s)" in out
        assert "Queued 1 subagent(s)" in out
        spawned_at, queued_at = out.index("Spawned 1"), out.index("Queued 1")
        assert out.index("s1") > spawned_at and out.index("s1") < queued_at
        assert out.index("q1") > queued_at

    def test_an_old_gateway_answer_still_reads_spawned(self) -> None:
        """No ``status`` at all: the pre-existing text, byte for byte."""
        out = _run([{"id": "s1"}, {"id": "s2"}])
        assert out.startswith("Spawned 2 subagent(s). Results will arrive as completion events:")
        assert "Queued" not in out

    def test_a_capacity_queued_answer_still_reads_spawned(self) -> None:
        out = _run([{"id": "s1", "status": "spawned"}])
        assert out.startswith("Spawned 1 subagent(s)")
        assert "Queued" not in out

    def test_a_queued_answer_without_detail_falls_back_to_the_reason_kind(self) -> None:
        out = _run([{"id": "q1", "status": "queued", "reason": "some_future_kind"}])
        assert out.splitlines()[0].startswith(
            "Queued 1 subagent(s). Not started yet: some_future_kind"
        )


class TestSpawnSubAgentsNamesTheDeferral:
    """The blocking sibling polls each id until it settles. A deferred row has
    no run yet and answers ``queued: true``: the member is reported ONCE, under
    the ``queued`` record with its reason, and never as an error too -- seeing
    both, a model trusted the error and dispatched the same work again."""

    @staticmethod
    def _call(monkeypatch, status: dict) -> list[dict]:
        import json

        clock = {"now": 0.0}

        class _Time:
            @staticmethod
            def monotonic() -> float:
                clock["now"] += 30.0  # each read burns the whole wait
                return clock["now"]

            @staticmethod
            def sleep(_s: float) -> None:
                pass

        def _post(path: str, body: dict, **_kw: object) -> dict:
            if path == "/api/spawn":
                return {
                    "id": "q1",
                    "status": "queued",
                    "reason": "low_memory",
                    "reason_detail": _DETAIL,
                }
            return {}

        def _get(path: str, **_kw: object) -> dict:
            return dict(status)

        monkeypatch.setenv("KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT", "60")
        with (
            patch.object(spawn_tools.mcp_core, "_post", side_effect=_post) as post,
            patch.object(spawn_tools.mcp_core, "_get", side_effect=_get),
            patch.object(spawn_tools.mcp_core, "time", _Time),
            patch.object(spawn_tools.mcp_core, "_resolve_session_key", return_value="chat-1"),
            patch.object(spawn_tools, "_hold_for_parent_resume", return_value=None),
            patch.object(spawn_tools, "is_tool_cancelled", return_value=False),
        ):
            out = spawn_tools.spawn_sub_agents(
                "spawn_sub_agents",
                {
                    "agents": [{"prompt": "summarize the log"}],
                    "solo_reason": "bulk_data",
                    "solo_details": "a large log only the summary of which is needed",
                },
            )
        # Never marked collected: it has not run, so its completion must inject.
        assert not [c for c in post.call_args_list if c.args[0] == "/api/spawn/mark-collected"]
        return [json.loads(chunk) for chunk in out.split("\n\n")]

    def test_a_not_found_answer_is_an_error_never_a_queued_member(self, monkeypatch) -> None:
        """ "Held" is read off ``queued: true`` only, never off a 404's prose: a
        gone row or a refused lookup is not "queued, nothing was cancelled"."""
        records = self._call(monkeypatch, {"error": "not found"})
        assert not [r for r in records if r.get("status") == "queued"]
        errors = [r for r in records if r.get("status") == "error"]
        assert len(errors) == 1 and "check spawn_status" in errors[0]["hint"]

    def test_a_capacity_wait_does_not_repeat_the_accept_time_sentence(self, monkeypatch) -> None:
        """Deferred for memory at accept, now waiting behind the concurrency cap
        with no sentence of its own: the record names the CURRENT wait."""
        records = self._call(
            monkeypatch,
            {"id": "q1", "done": False, "queued": True, "reason": "concurrency_limit"},
        )
        queued = [r for r in records if r.get("status") == "queued"]
        assert queued and queued[0]["agents"] == {
            "q1": "waiting for a free slot behind the concurrency limit"
        }
        assert _DETAIL not in json.dumps(records)

    def test_a_run_waiting_to_resume_is_running_work_not_unstarted(self, monkeypatch) -> None:
        records = self._call(
            monkeypatch,
            {
                "id": "q1",
                "done": False,
                "queued": True,
                "resuming": True,
                "resuming_reason": "gateway_restart",
            },
        )
        assert not [r for r in records if r.get("status") == "queued"]
        still = [r for r in records if r.get("status") == "still_running"]
        assert still and still[0]["states"] == {"q1": "waiting_to_resume"}

    def test_a_queued_answer_is_reported_once_with_the_latest_reason(self, monkeypatch) -> None:
        later = "low memory: 1.1 GB available, need 4 GB"
        records = self._call(
            monkeypatch,
            {"id": "q1", "done": False, "queued": True, "reason_detail": later},
        )
        assert [r["agents"] for r in records if r.get("status") == "queued"] == [{"q1": later}]
        assert not [r for r in records if r.get("status") == "error"]
        assert not [r for r in records if r.get("status") == "still_running"]

    def test_a_deferred_member_that_started_is_running_not_queued(self, monkeypatch) -> None:
        """Started after its deferral and still going when the wait ended: it is
        a running child, so the never-started record must not name it."""
        records = self._call(monkeypatch, {"id": "q1", "done": False, "turns": 3})
        still = [r for r in records if r.get("status") == "still_running"]
        assert still and still[0]["states"] == {"q1": "running"}
        assert not [r for r in records if r.get("status") == "queued"]

    def test_a_transport_failure_is_still_an_error(self, monkeypatch) -> None:
        """An unreachable gateway says nothing about the row: the member keeps
        its error entry, and because it was accepted (deferred) at spawn time,
        a hint not to re-spawn it blind."""
        records = self._call(monkeypatch, {"error": "connection refused"})
        errors = [r for r in records if r.get("status") == "error"]
        assert len(errors) == 1
        assert errors[0]["hint"] == (
            "accepted at spawn time; its state couldn't be read now; "
            "check spawn_status before re-spawning"
        )
        assert not [r for r in records if r.get("status") == "queued"]


class TestSpawnSubAgentsWaitsOnlyForStartedWork:
    """A member still queued and not started settles the WAIT: the call returns
    once every member is done, errored or queued, and only members not yet
    settled are polled again."""

    def test_a_queued_member_does_not_hold_the_call(self, monkeypatch) -> None:
        import json

        class _Time:
            now = 0.0

            @classmethod
            def monotonic(cls) -> float:
                cls.now += 0.001  # a deadline the loop could spin against for hours
                return cls.now

            @staticmethod
            def sleep(_s: float) -> None:
                pass

        ids = iter(["a1", "b1", "q1"])
        gets: dict[str, int] = {}
        polls_of_b = {"n": 0}

        def _post(path: str, body: dict, **_kw: object) -> dict:
            if path == "/api/spawn":
                aid = next(ids)
                return {
                    "id": aid,
                    **({"status": "queued", "reason": "low_memory"} if aid == "q1" else {}),
                }
            return {}

        def _get(path: str, **_kw: object) -> dict:
            aid = path.rsplit("/", 1)[-1]
            gets[aid] = gets.get(aid, 0) + 1
            if aid == "a1":
                return {"id": "a1", "done": True, "result": "A"}
            if aid == "b1":
                polls_of_b["n"] += 1
                done = polls_of_b["n"] >= 3
                return {"id": "b1", "done": done, **({"result": "B"} if done else {})}
            return {
                "id": "q1",
                "done": False,
                "queued": True,
                "reason": "low_memory",
                "reason_detail": "low memory: 1.1 GB available, need 4 GB",
            }

        monkeypatch.setenv("KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT", "7200")
        with (
            patch.object(spawn_tools.mcp_core, "_post", side_effect=_post),
            patch.object(spawn_tools.mcp_core, "_get", side_effect=_get),
            patch.object(spawn_tools.mcp_core, "time", _Time),
            patch.object(spawn_tools.mcp_core, "_resolve_session_key", return_value="chat-1"),
            patch.object(spawn_tools, "_hold_for_parent_resume", return_value=None),
            patch.object(spawn_tools, "is_tool_cancelled", return_value=False),
        ):
            out = spawn_tools.spawn_sub_agents(
                "spawn_sub_agents",
                {
                    "agents": [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "q"}],
                    "solo_reason": "bulk_data",
                    "solo_details": "three independent summaries",
                },
            )
        records = [json.loads(chunk) for chunk in out.split("\n\n")]
        assert [r["status"] for r in records if r.get("agent")] == ["completed", "completed"]
        assert [r["agents"] for r in records if r.get("status") == "queued"] == [
            {"q1": "low memory: 1.1 GB available, need 4 GB"}
        ]
        # a1 settled on the first pass and was not polled again (+1 final read).
        assert gets["a1"] == 2
        # q1 was read once it was reached in the wait, then once in the result.
        assert gets["q1"] == 2


class TestSpawnSubAgentsWaitsOutACapacityQueue:
    def test_a_member_queued_behind_the_cap_is_waited_for(self, monkeypatch) -> None:
        """A wave larger than the cap: the tail answers ``queued`` with
        ``concurrency_limit`` and drains with the wave, so the call keeps
        polling it and returns its result rather than a queued record."""
        import json

        class _Time:
            now = 0.0

            @classmethod
            def monotonic(cls) -> float:
                cls.now += 0.001
                return cls.now

            @staticmethod
            def sleep(_s: float) -> None:
                pass

        polls = {"n": 0}

        def _post(path: str, body: dict, **_kw: object) -> dict:
            return {"id": "c1", "status": "spawned"} if path == "/api/spawn" else {}

        def _get(path: str, **_kw: object) -> dict:
            polls["n"] += 1
            if polls["n"] < 4:
                return {"id": "c1", "done": False, "queued": True, "reason": "concurrency_limit"}
            return {"id": "c1", "done": True, "result": "C"}

        monkeypatch.setenv("KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT", "7200")
        with (
            patch.object(spawn_tools.mcp_core, "_post", side_effect=_post),
            patch.object(spawn_tools.mcp_core, "_get", side_effect=_get),
            patch.object(spawn_tools.mcp_core, "time", _Time),
            patch.object(spawn_tools.mcp_core, "_resolve_session_key", return_value="chat-1"),
            patch.object(spawn_tools, "_hold_for_parent_resume", return_value=None),
            patch.object(spawn_tools, "is_tool_cancelled", return_value=False),
        ):
            out = spawn_tools.spawn_sub_agents(
                "spawn_sub_agents",
                {
                    "agents": [{"prompt": "c"}],
                    "solo_reason": "bulk_data",
                    "solo_details": "one summary",
                },
            )
        records = [json.loads(chunk) for chunk in out.split("\n\n")]
        assert [r["status"] for r in records if r.get("agent")] == ["completed"]
        assert not [r for r in records if r.get("status") == "queued"]


class TestSpawnStatusAndListShowAQueuedRun:
    """``spawn_status`` / ``spawn_list`` render a spawn that has no run yet as
    queued, with its reason, rather than "not found" / "No subagents running."."""

    def test_spawn_status_says_queued_and_why(self) -> None:
        answer = {
            "id": "q1",
            "task": "summarize",
            "done": False,
            "queued": True,
            "elapsed": 12,
            "reason": "low_memory",
            "reason_detail": _DETAIL,
        }
        with patch.object(spawn_tools.mcp_core, "_get", return_value=answer):
            out = spawn_tools.spawn_status("spawn_status", {"agent_id": "q1"})
        assert out.splitlines()[0] == "[QUEUED · 12s]"
        assert _DETAIL in out
        assert "do not spawn it again" in out
        assert "RUNNING" not in out and "Error" not in out

    def test_spawn_status_names_the_kind_without_a_sentence(self) -> None:
        answer = {"id": "q1", "done": False, "queued": True, "reason": "concurrency_limit"}
        with patch.object(spawn_tools.mcp_core, "_get", return_value=answer):
            out = spawn_tools.spawn_status("spawn_status", {"agent_id": "q1"})
        assert out.startswith("[QUEUED]")
        assert "concurrency limit" in out

    def test_spawn_list_lists_queued_rows_apart_from_runs(self) -> None:
        answer = {
            "agents": [],
            "queued": [
                {"id": "q1", "task": "summarize the log", "reason_detail": _DETAIL},
                {"id": "q2", "task": "second", "reason": "concurrency_limit"},
            ],
        }
        with (
            patch.object(spawn_tools.mcp_core, "_get", return_value=answer),
            patch.object(spawn_tools.mcp_core, "list_agents", return_value=[]),
        ):
            out = spawn_tools.spawn_list("spawn_list", {})
        assert "No subagents running." not in out
        assert f"q1  [queued] (not started: {_DETAIL})  summarize the log" in out
        assert "q2  [queued] (not started: waiting for a free slot behind the concurrency" in out

    def test_spawn_list_says_when_the_queued_list_is_partial(self) -> None:
        answer = {
            "agents": [],
            "queued": [{"id": "q1", "task": "oldest", "reason": "low_memory"}],
            "queued_truncated": True,
        }
        with (
            patch.object(spawn_tools.mcp_core, "_get", return_value=answer),
            patch.object(spawn_tools.mcp_core, "list_agents", return_value=[]),
        ):
            out = spawn_tools.spawn_list("spawn_list", {})
        assert "the queued list is partial" in out
        assert "do not spawn them again" in out

    def test_spawn_list_with_nothing_running_or_queued_is_unchanged(self) -> None:
        with (
            patch.object(spawn_tools.mcp_core, "_get", return_value={"agents": []}),
            patch.object(spawn_tools.mcp_core, "list_agents", return_value=[]),
        ):
            out = spawn_tools.spawn_list("spawn_list", {})
        assert out.splitlines()[0] == "No subagents running."
