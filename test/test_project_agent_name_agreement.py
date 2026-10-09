"""The project-agent name readers agree, and an empty declared name is not a name.

Lives in its own module rather than beside the other discovery tests: that file is
not black-clean, so touching it would make this change reformat it whole.
"""

from __future__ import annotations

import json


def test_an_empty_declared_name_neither_lists_nor_shadows(tmp_path):
    """A spec declaring ``{"name": ""}`` contributes its FILENAME, never ``""``.

    ``spec_str`` returns a present key verbatim, so an empty ``name`` is a value
    the readers have to handle rather than pass on. It is not a name the backend
    can activate, and ``agent_binding_is_shadowed`` is called with
    ``effective=""`` whenever the loader allows the project override — so ``""``
    in the allowlist answers for a name no caller supplied and reports every
    binding as shadowed, skipping member-bound fires. The singular reader applies
    the filename fallback; both readers agree on it.
    """
    from kiro_crew.agent_discovery import (
        agent_binding_is_shadowed,
        project_agent_name,
        project_agent_names,
    )

    agents = tmp_path / ".kiro" / "agents"
    agents.mkdir(parents=True)
    spec = agents / "blank-kirocrew.json"
    spec.write_text(json.dumps({"name": ""}), encoding="utf-8")

    names = project_agent_names(tmp_path, operation="t", source="unknown")
    assert "" not in names, "an empty name must never enter the dispatch allowlist"
    assert project_agent_name(spec) in names, "the plural must agree with the singular"

    # The comparison refuses an empty candidate on its own, so neither side of
    # the producer/consumer pair has to hold alone.
    assert agent_binding_is_shadowed(names, "", "") is False
    assert agent_binding_is_shadowed({""}, "", "") is False
