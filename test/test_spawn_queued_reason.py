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

from unittest.mock import patch

from kiro_crew.mcp_tools import spawn as spawn_tools

_DETAIL = "low memory: 3.2 GB available, need 4 GB (0.5 GB per warming start)"


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
        out = _run([{"id": "q1", "status": "queued", "reason": "adaptive_cap_zero"}])
        assert out.splitlines()[0].startswith(
            "Queued 1 subagent(s). Not started yet: adaptive_cap_zero"
        )


class TestSpawnSubAgentsNamesTheDeferral:
    """The blocking sibling polls each id until it settles. A deferred row is
    never registered as a run, so its poll answers ``not found`` and the caller
    saw an error entry with no cause; the accept-time reason is now reported
    beside it under its own ``queued`` line."""

    def test_a_member_deferred_at_accept_is_reported_queued_with_its_reason(
        self, monkeypatch
    ) -> None:
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
            return {"error": "not found"}

        monkeypatch.setenv("KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT", "60")
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
                    "agents": [{"prompt": "summarize the log"}],
                    "solo_reason": "bulk_data",
                    "solo_details": "a large log only the summary of which is needed",
                },
            )
        records = [json.loads(chunk) for chunk in out.split("\n\n")]
        queued = [r for r in records if r.get("status") == "queued"]
        assert len(queued) == 1
        assert queued[0]["agents"] == {"q1": _DETAIL}
