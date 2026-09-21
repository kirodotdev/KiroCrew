"""The packaged agent-spec field reference's skill-resource claims are pinned to the code.

``scripts/docs_lint.py`` gates a doc's SYMBOLS and PATHS, never what it claims about
them, and ``test_context_management_doc.py`` pins one file:
``docs/architecture/context-management.md``. That leaves
``src/kiro_crew/docs/agent-spec-fields.md`` free to say the opposite of the code with
every symbol and path resolving and the lint green — and the claims most exposed to
that are its skill-resource ones, because they describe a decision whose inputs are
not visible in any symbol it names.

This module closes that gap for that file. Where a claim is about what a function
DOES, it is asserted by calling that function, so a behaviour-preserving rewrite stays
green and a behaviour change fails. Where it is about what a function SAYS — an
expression the page quotes — the assertion is over source text, anchored to the whole
construct rather than to a token that recurs. Either way each is paired with the
sentence in the page that states it, so a code change cannot pass by leaving the prose
behind.

One claim is deliberately NOT executed here: the four-row injection table, which
``test_context_management_doc.py`` already runs against ``_skills_injection_plan``
for the sibling page stating the same four rows. This module pins that page's wording
and leaves the matrix there, so the two pages cannot be pinned to two drifting copies
of one truth table.
"""

from pathlib import Path

import pytest

DOC = Path(__file__).parent.parent / "src" / "kiro_crew" / "docs" / "agent-spec-fields.md"


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC.read_text(encoding="utf-8")


def test_the_doc_does_not_claim_a_backend_condition_on_skill_injection(doc_text: str) -> None:
    """``is_cc`` decides steering, not skills, and the page must not say otherwise.

    The wording is what this pins. The truth table behind it is executed once, in
    ``test_context_management_doc.py::test_skill_injection_table_matches_the_plan_function``,
    which asserts both halves of the return for every row on both backends; a second
    copy of that matrix here would only add a place for the two to disagree.
    """
    table = doc_text.split("### Resources", 1)[1].split("## Per-surface summary", 1)[0]
    for stale in ("nothing injected", "kiro-cli loads them natively"):
        assert stale not in table, (
            f"§Resources still says {stale!r}; a mapped skill:// is injected by Crew "
            "on every backend since _skills_injection_plan stopped reading is_cc"
        )
    assert "| `kirocrew` | mapped | mapped set only, injected by Crew |" in table
    assert "| custom | mapped | mapped set only, injected by Crew |" in table


def test_steering_is_still_the_backend_gated_half(doc_text: str) -> None:
    """The `file://` half IS backend-gated, so the page must keep saying so.

    The whole gate expression is matched as one contiguous block, ending at the
    ``_load_steering_resources()`` call it guards. ``is_cc`` is read at four sites in
    that module, so a token match anywhere would keep this green with the steering
    gate's own condition dropped.
    """
    source = (Path(__file__).parent.parent / "src" / "kiro_crew" / "context.py").read_text(
        encoding="utf-8"
    )
    gate = (
        "            not essentials\n"
        "            and not is_custom\n"
        "            and is_cc\n"
        "            and _group_included(context_groups, CONTEXT_GROUP_PROJECT)\n"
        "        ):\n"
        "            steering_ctx = _load_steering_resources()\n"
    )
    assert gate in source, (
        "the steering block's gate no longer reads is_cc together with the default-agent "
        "and project-group conditions, so the page's 'steering is the backend-gated half' "
        "is stale"
    )
    assert "The `file://` steering block does NOT follow that shape." in doc_text


def test_relative_skill_uri_anchors_at_the_supplied_project(tmp_path: Path) -> None:
    """The doc's relative-URI rule: project_dir wins, spec location is the fallback."""
    from kiro_crew.agent_discovery import expand_skill_uri

    spec = tmp_path / "proj" / ".kiro" / "agents" / "foo.json"
    spec.parent.mkdir(parents=True)
    spec.write_text("{}", encoding="utf-8")
    other = tmp_path / "other"

    assert expand_skill_uri("skill://rel/*/SKILL.md", spec) == str(
        tmp_path / "proj" / "rel/*/SKILL.md"
    )
    assert expand_skill_uri("skill://rel/*/SKILL.md", spec, project_dir=other) == str(
        other / "rel/*/SKILL.md"
    )
    paragraph = DOC.read_text(encoding="utf-8").split("`skill://<glob>` maps skills", 1)[1]
    paragraph = paragraph.split("`file://<glob>`", 1)[0]
    assert (
        "A relative glob anchors at the `project_dir` the caller\n"
        "supplies — the session's own project, on every path that resolves skills for a\n"
        "prompt — and only with no project supplied does it fall back to three levels above\n"
        "the spec file" in paragraph
    ), "the relative-glob rule must state project-first WITH its fallback, in that order"
    assert "anything else\nworkspace-relative, anchored three levels above" not in paragraph, (
        "the page states the spec-location anchor unconditionally again; it is the "
        "fallback, not the rule"
    )


def test_native_launch_view_carries_no_skill_resources(doc_text, tmp_path, monkeypatch) -> None:
    """The kiro-cli column rests on the native view dropping every ``skill://``.

    Driven through ``prepare_native_skill_projection`` rather than through its source
    text: the filter's expression appears twice in that module, so grepping for it
    stays green with the filtering occurrence deleted.
    """
    import json
    from types import SimpleNamespace

    from kiro_crew.acp import skill_projection as projection

    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    agents = home / "agents"
    agents.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda name: {"command": "test-core", "args": []},
    )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    (agents / "custom.json").write_text(
        json.dumps(
            {
                "name": "custom",
                "resources": ["file://RULES.md", "skill://catalog/*/SKILL.md"],
            }
        ),
        encoding="utf-8",
    )

    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads((agents / f"{prepared.agent('custom')}.json").read_text(encoding="utf-8"))
    assert [r for r in view["resources"] if r.startswith("skill://")] == [], (
        "the native launch view now carries skill:// resources, so the doc's "
        "kiro-cli cell is stale"
    )
    assert "the native launch view carries no `skill://`" in doc_text


def test_mirror_note_names_the_real_return_expression(doc_text: str) -> None:
    """The mirror note quotes the plan's expression; a rewrite must update the page."""
    source = (Path(__file__).parent.parent / "src" / "kiro_crew" / "context.py").read_text(
        encoding="utf-8"
    )
    assert "return (bool(globs) or not is_custom), globs" in source
    assert "`bool(globs) or not is_custom`" in doc_text
