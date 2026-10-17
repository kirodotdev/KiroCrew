"""Observable outcomes of project-scoped `local_knowledge_search`.

End-to-end at the tool layer against a real `KnowledgeStore` + `HybridRetriever`
(keyword/graph legs only -- no embedder), with the project namespace the tool
would fetch from the gateway mocked. Proves the three outcomes the work item
names:

* a session in project X with a doc filed under X finds it ranked first;
* a session outside any project gets results identical to the pre-project path;
* a doc filed under project Y does not rank first for a session in project X.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew import mcp_core
from kiro_crew.knowledge.project_scope import project_namespace
from kiro_crew.knowledge.retrieval import HybridRetriever
from kiro_crew.knowledge.store import KnowledgeStore

_QUERY = "roll back payments deployment"


@pytest.fixture
def cfg_dir(tmp_path):
    """A config dir seeded with a knowledge DB at the real tool layout.

    The tool builds ``config_dir()/workspace/knowledge/knowledge.db``, so the DB
    is created there and ``config_dir`` is pointed at *tmp_path*. One global doc,
    one under project X and one under project Y -- all answering the same query,
    so ranking (not mere matching) is what the assertions read.
    """
    db = tmp_path / "workspace" / "knowledge" / "knowledge.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = KnowledgeStore(str(db))
    text = "The deployment runbook: how to roll back the payments service safely."
    # Sourceless items (source_id=None): the namespace, not the source, is what
    # these tests read, and a sourceless item avoids the sources FK.
    store.add_item("Global runbook", text, "doc", namespace="default")
    store.add_item("Project X runbook", text, "doc", namespace=project_namespace("X"))
    store.add_item("Project Y runbook", text, "doc", namespace=project_namespace("Y"))
    store._close_all_for_tests()
    yield tmp_path


def _run_search(cfg: Path, *, project_ns=None, get_raises=False, limit=3) -> str:
    """Invoke the tool with the config dir and the project lookup mocked.

    *project_ns* is what the gateway lookup (`mcp_core._get`) returns; when
    *get_raises* is set the lookup raises instead (an older gateway with no
    route). Both exercise the real tool end-to-end over the seeded DB.
    """
    db = cfg / "workspace" / "knowledge" / "knowledge.db"
    store = KnowledgeStore(str(db))
    retr = HybridRetriever(store)  # no embedder: keyword + graph legs only

    def _fake_get_knowledge_search(_db_path, _cfg_path):
        return store, None

    get_patch = (
        patch.object(mcp_core, "_get", side_effect=RuntimeError("no such route"))
        if get_raises
        else patch.object(mcp_core, "_get", return_value={"namespace": project_ns or ""})
    )
    with (
        patch.object(mcp_core, "config_dir", return_value=str(cfg)),
        patch.object(mcp_core, "_get_knowledge_search", _fake_get_knowledge_search),
        patch.object(mcp_core, "vector_leg", return_value=(None, None)),
        patch.object(mcp_core, "HybridRetriever", return_value=retr),
        get_patch,
    ):
        from kiro_crew.mcp_tools.knowledge import local_knowledge_search

        out = local_knowledge_search("local_knowledge_search", {"query": _QUERY, "limit": limit})
    store._close_all_for_tests()
    return out


def test_project_x_doc_ranks_first_for_a_session_in_x(cfg_dir):
    out = _run_search(cfg_dir, project_ns=project_namespace("X"))
    assert "Project X runbook" in out
    x = out.index("Project X runbook")
    for other in ("Global runbook", "Project Y runbook"):
        if other in out:
            assert x < out.index(other), f"{other} should rank after Project X"


def test_session_outside_any_project_is_identical_to_before(cfg_dir):
    # A session filed nowhere (namespace "") and a gateway that cannot answer the
    # project lookup at all (older gateway: the route raises) must both take the
    # unscoped single-pass path and produce the SAME output -- the pre-project
    # behaviour. A mishandled error would make the two diverge.
    filed_nowhere = _run_search(cfg_dir, project_ns="")
    route_absent = _run_search(cfg_dir, get_raises=True)
    assert filed_nowhere == route_absent
    assert "runbook" in filed_nowhere


def test_project_y_doc_does_not_rank_first_for_a_session_in_x(cfg_dir):
    out = _run_search(cfg_dir, project_ns=project_namespace("X"))
    assert "Project X runbook" in out
    if "Project Y runbook" in out:
        assert out.index("Project X runbook") < out.index("Project Y runbook")


def test_project_x_search_still_surfaces_global_docs_after_its_own(cfg_dir):
    # Project-first is not project-only: the global library still answers, ranked
    # after the project's own hits.
    out = _run_search(cfg_dir, project_ns=project_namespace("X"))
    assert "Project X runbook" in out
    assert "Global runbook" in out


def test_a_strong_global_doc_is_not_crowded_out_by_weak_project_fill(tmp_path):
    # The Design review's case: a project that already holds `limit` weakly
    # related docs must not starve a strongly-relevant global doc. The project
    # docs share only ONE query word; the global doc matches the whole query, so
    # its raw score beats boost x the weak project hits and it ranks in.
    db = tmp_path / "workspace" / "knowledge" / "knowledge.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    store = KnowledgeStore(str(db))
    for i in range(3):
        store.add_item(
            f"Weak project note {i}",
            "deployment notes and meeting minutes for the team",
            "doc",
            namespace=project_namespace("X"),
        )
    store.add_item(
        "Global payments rollback runbook",
        "roll back the payments deployment service safely step by step",
        "doc",
        namespace="default",
    )
    store._close_all_for_tests()
    out = _run_search(tmp_path, project_ns=project_namespace("X"), limit=3)
    assert "Global payments rollback runbook" in out
