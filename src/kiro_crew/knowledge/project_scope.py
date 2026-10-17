"""Project-scoped knowledge: the folder-id -> knowledge-namespace mapping.

A Kiro Crew project is a sidebar folder (`dashboard/chat_folders.py`). Claude
Projects give each project its own knowledge base; here that is expressed by
filing a session's knowledge under a per-project NAMESPACE on the Knowledge
Library's existing ``items.namespace`` column (the organisational label the
store, the dashboard browse filter and ``HybridRetriever.search(namespace=)``
already use). This module owns the one rule that turns a folder id into that
label, and nothing else: it is a pure string function with no store, no folder
tree and no I/O, so both the gateway and the stateless ``mcp-core`` server can
import it without pulling in the dashboard HTTP tree.

The namespace is DERIVED from the folder id, never stored on the folder: sibling
item C owns the folder schema, and a derivation keeps this feature from adding a
field to it. The derivation is deliberately total and reversible in shape (a
fixed prefix) so a value can be recognised as project-scoped by inspection and a
namespace browse in the dashboard reads sensibly.

This is a RELEVANCE / organisation filter, exactly like the namespace the folder
sources already carry -- NOT a security boundary. It narrows which documents
seed a search and which namespace an agent-added document lands in; it never
gates who may read what. The security boundary stays where it already is (the
knowledge owner gate on the mutating routes, the governed tool gate).

The project a session belongs to is the ROOT of its folder's ancestor chain,
resolved by :func:`kiro_crew.dashboard.folder_ownership.project_root_folder_id`:
the sidebar nests at most Project > sub-team > workers, and the project is the
top-level team-lead folder, so a session filed in a child folder searches and
writes the same project knowledge as one filed in the project folder itself.
Resolving the root here (rather than the leaf) is what makes a sub-team or a
worker inherit its project's knowledge instead of forking a private slice of it.
"""

from __future__ import annotations

#: Prefix on every project-scoped knowledge namespace. The value after it is the
#: ROOT project folder's id. Chosen so a project namespace is greppable and never
#: collides with a folder source's own ``properties["namespace"]`` (which
#: defaults to ``"default"`` and is otherwise user-named free text): a colon is
#: not produced by the folder-source namespace UI, and no folder id is literally
#: ``"default"``.
PROJECT_NAMESPACE_PREFIX = "project:"


def project_namespace(folder_id: str) -> str:
    """The knowledge namespace for the project rooted at *folder_id*.

    *folder_id* MUST be the ROOT project folder id (see
    ``folder_ownership.project_root_folder_id``); passing a nested child id here
    would scope to the child rather than the project, which is the inheritance
    this feature exists to provide. Returns ``""`` for an empty id -- a session
    filed in no folder has no project namespace, and the search then runs exactly
    as it did before this feature (a single unscoped pass).
    """
    folder_id = (folder_id or "").strip()
    if not folder_id:
        return ""
    return f"{PROJECT_NAMESPACE_PREFIX}{folder_id}"
