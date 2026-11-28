"""A workflow fallback tab leaves the tracked-changes list on disk.

The bound workflow fallback re-creates a tab on the originating transcript and reads
its line for the turn time limit and the chips-off switch, but not the unlinked set.
The tracked list is only shown beside those tombstones, so the tab keeps the list
unread: its saves carry the stored list forward and it shows none.
"""

from unittest.mock import MagicMock

import pytest
from test_workflows_private_outputs import State
from test_workflows_private_outputs import world as _world

from kiro_crew.dashboard import tracked_changes as tc
from kiro_crew.dashboard.source_providers.contract import source_ref_identity_key
from kiro_crew.dashboard.workflow_inject import inject_bound_workflow_result
from kiro_crew.workflow_memory import WorkflowScope

world = _world

KEY = source_ref_identity_key(("github", "github.com", "acme", "widgets", 7, "", "change", ""))
LINE = {
    tc.LINE_KEY: [
        {
            "identity": KEY,
            "url": "https://github.com/acme/widgets/pull/7",
            "sources": [{"kind": "created", "ref": ""}],
            "added_at": "2026-01-01T00:00:00+00:00",
        }
    ],
    # The person unlinked it; this binder does not read the set.
    "dismissed_source_links": [KEY],
}


@pytest.mark.asyncio
async def test_a_workflow_fallback_keeps_the_list_unread(world):
    scope = await WorkflowScope.admit("wf_tracked", world.builder, "dashboard:alice")
    state = State()
    state.conversation_log = MagicMock()
    state.conversation_log.get_metadata_status = MagicMock(return_value=(LINE, True))
    snapshot = {
        "session_key": scope.origin,
        "run_id": scope.run_id,
        "status": "finished",
        "result": "RESULT",
        "execution_context": scope.execution_context.to_record(),
    }
    assert await inject_bound_workflow_result(state, scope.run_id, snapshot)
    slot = state.get_slot("workflow-wf_tracked")
    assert slot._tracked_hydrated is False
    assert slot._tracked_changes == {}
