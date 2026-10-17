"""The folder-id -> project-knowledge-namespace mapping and the root walk.

Pure functions, no store and no I/O: this file pins the derivation
(`knowledge.project_scope`) and the folder-tree root resolution
(`folder_ownership.project_root_folder_id`) that together decide which project
namespace a session's knowledge is filed under.
"""

from __future__ import annotations

from kiro_crew.dashboard.folder_ownership import project_root_folder_id
from kiro_crew.knowledge.project_scope import PROJECT_NAMESPACE_PREFIX, project_namespace


def test_namespace_is_prefix_plus_folder_id():
    assert project_namespace("f_abc") == f"{PROJECT_NAMESPACE_PREFIX}f_abc"


def test_empty_folder_id_has_no_project_namespace():
    # A session filed in no folder gets "", which the search path reads as
    # "global scope" -- byte-identical to the pre-project behaviour.
    assert project_namespace("") == ""
    assert project_namespace("   ") == ""


def test_folder_id_is_stripped():
    assert project_namespace("  f_abc  ") == f"{PROJECT_NAMESPACE_PREFIX}f_abc"


def test_prefix_never_collides_with_a_folder_source_namespace():
    # Folder sources default to "default" and are otherwise user-named free text;
    # the project prefix carries a colon the folder-source UI does not produce.
    assert not project_namespace("f_abc") == "default"
    assert ":" in PROJECT_NAMESPACE_PREFIX
    assert project_namespace("f_abc").startswith(PROJECT_NAMESPACE_PREFIX)


# ---- root walk ---------------------------------------------------------------

_TREE = [
    {"id": "root"},
    {"id": "child", "parent_id": "root"},
    {"id": "grandchild", "parent_id": "child"},
    {"id": "other_root"},
]


def test_root_of_a_top_level_folder_is_itself():
    assert project_root_folder_id(_TREE, "root") == "root"


def test_nested_folder_resolves_to_its_project_root():
    # The inheritance this feature exists for: a sub-team / worker folder resolves
    # to the top-level project, so it searches the project's knowledge.
    assert project_root_folder_id(_TREE, "child") == "root"
    assert project_root_folder_id(_TREE, "grandchild") == "root"


def test_sibling_projects_do_not_merge():
    assert project_root_folder_id(_TREE, "other_root") == "other_root"


def test_empty_folder_id_roots_to_empty():
    assert project_root_folder_id(_TREE, "") == ""


def test_unknown_folder_id_roots_to_itself():
    # A session pointing at a deleted folder still gets a stable, non-empty
    # namespace rather than silently collapsing to the global scope.
    assert project_root_folder_id(_TREE, "ghost") == "ghost"


def test_dangling_parent_stops_at_highest_existing_ancestor():
    tree = [{"id": "x", "parent_id": "gone"}]
    assert project_root_folder_id(tree, "x") == "x"


def test_cycle_does_not_hang_and_returns_a_stable_id():
    cyc = [{"id": "a", "parent_id": "b"}, {"id": "b", "parent_id": "a"}]
    assert project_root_folder_id(cyc, "a") in {"a", "b"}


def test_malformed_rows_are_ignored():
    tree = [{"id": "root"}, "not-a-dict", {"id": None}, {"id": "child", "parent_id": "root"}]
    assert project_root_folder_id(tree, "child") == "root"


def test_end_to_end_nested_session_gets_the_project_namespace():
    root = project_root_folder_id(_TREE, "grandchild")
    assert project_namespace(root) == "project:root"
