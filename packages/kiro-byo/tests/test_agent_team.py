from __future__ import annotations

import unittest

from kiro_byo.agent_runtime import AgentRegistry, AgentSpec
from kiro_byo.agent_team import AgentTeamRegistry


class AgentTeamTests(unittest.TestCase):
    def test_team_board_message_and_bounded_dynamic_spawn(self) -> None:
        agents = AgentRegistry()
        plan_id, tasks = agents.begin_plan(
            "s1",
            (
                AgentSpec(role="lead", name="lead", objective="lead", authority="writer"),
                AgentSpec(role="worker", name="worker-1", objective="inspect", tool_policy="read_only"),
            ),
        )
        leader, worker = tasks
        teams = AgentTeamRegistry()
        team = teams.create_for_plan(
            session_id="s1", plan_id=plan_id, leader_task_id=leader.task_id,
            member_task_ids=[leader.task_id, worker.task_id], max_agents=3,
            max_parallel=2, max_depth=1,
            objectives={leader.task_id: "lead", worker.task_id: "inspect"},
        )
        msg = teams.post_message(
            team.team_id, sender_task_id=leader.task_id,
            recipient_task_id=worker.task_id, text="check edge cases",
        )
        self.assertIsNotNone(msg)
        self.assertIsNone(teams.post_message(
            team.team_id, sender_task_id="outsider", recipient_task_id=worker.task_id, text="x"
        ))

        child = agents.spawn_task(
            plan_id=plan_id, session_id="s1",
            spec=AgentSpec(role="specialist", name="spec", objective="alternate", tool_policy="read_only"),
            parent_task_id=leader.task_id, max_agents=3, max_depth=1,
        )
        self.assertEqual(child.depth, 1)
        self.assertTrue(teams.add_member(team.team_id, child.task_id))
        with self.assertRaises(ValueError):
            agents.spawn_task(
                plan_id=plan_id, session_id="s1",
                spec=AgentSpec(role="worker", name="too-many", objective="x", tool_policy="read_only"),
                parent_task_id=leader.task_id, max_agents=3, max_depth=1,
            )

    def test_nested_depth_is_bounded_and_terminal_member_can_be_recycled(self) -> None:
        agents = AgentRegistry()
        plan_id, tasks = agents.begin_plan(
            "s2", (AgentSpec(role="lead", name="lead", objective="lead"),)
        )
        leader = tasks[0]
        child = agents.spawn_task(
            plan_id=plan_id, session_id="s2",
            spec=AgentSpec(role="worker", name="child", objective="child", tool_policy="read_only"),
            parent_task_id=leader.task_id, max_agents=4, max_depth=2,
        )
        grandchild = agents.spawn_task(
            plan_id=plan_id, session_id="s2",
            spec=AgentSpec(role="worker", name="grand", objective="grand", tool_policy="read_only"),
            parent_task_id=child.task_id, max_agents=4, max_depth=2,
        )
        self.assertEqual(grandchild.depth, 2)
        with self.assertRaises(ValueError):
            agents.spawn_task(
                plan_id=plan_id, session_id="s2",
                spec=AgentSpec(role="worker", name="too-deep", objective="x", tool_policy="read_only"),
                parent_task_id=grandchild.task_id, max_agents=5, max_depth=2,
            )
        agents.transition(child.task_id, "completed")
        agents.transition(grandchild.task_id, "completed")
        self.assertFalse(agents.remove_task(child.task_id), "parents with live descendants must not be recycled")
        self.assertTrue(agents.remove_task(grandchild.task_id))
        self.assertTrue(agents.remove_task(child.task_id))
        self.assertIsNone(agents.get(child.task_id))


if __name__ == "__main__":
    unittest.main()
