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
    source = (
        Path(__file__).parent.parent / "src" / "kiro_crew" / "context_assembly" / "session_start.py"
    ).read_text(encoding="utf-8")
    gate = (
        "        not essentials\n"
        "        and not is_custom\n"
        "        and is_cc\n"
        "        and ctx._group_included(context_groups, ctx.CONTEXT_GROUP_PROJECT)\n"
        "    ):\n"
        "        steering_ctx = ctx._load_steering_resources()\n"
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


def test_inherited_trees_are_promised_for_kiro_cli_only(doc_text: str) -> None:
    """The inherited-trees cell sits in a column that names kiro-cli and no other harness.

    ``_skills_injection_plan`` lends the trees only when ``provider_type`` is
    ``PROVIDER_ACP``, and a KAS session's label is ``kas``. The behaviour is pinned by
    ``test_inherited_skill_scope.py``; this pins that the table's column for the
    inherited row does not also promise it to KAS.
    """
    from kiro_crew.acp.types import PROVIDER_LABEL_KAS
    from kiro_crew.agent_sdk.provider_identity import PROVIDER_ACP

    assert PROVIDER_LABEL_KAS != PROVIDER_ACP
    table = doc_text.split("### Resources", 1)[1].split("## Per-surface summary", 1)[0]
    lines = table.splitlines()
    at = next(i for i, line in enumerate(lines) if line.startswith("| custom | none |"))
    start = at
    while start > 0 and lines[start - 1].startswith("|"):
        start -= 1
    header = [cell.strip() for cell in lines[start].strip("|").split("|")]
    inherited = lines[at]
    cells = [cell.strip() for cell in inherited.strip("|").split("|")]
    column = header[cells.index("inherited skill trees, injected by Crew")]
    assert column == "kiro-cli", (
        f"the inherited-trees cell sits under {column!r}; only a kiro-cli session gets "
        "the trees, so the column must not name KAS or any other harness"
    )
    kas_column = next(name for name in header if "KAS" in name)
    assert cells[header.index(kas_column)] == "nothing"


def test_app_skill_mapping_names_the_namespaced_path(
    doc_text, tmp_path, monkeypatch, opened
) -> None:
    """The page's app-skill rule: one namespaced entry per skill, never the flat link.

    Driven through ``_register_skills`` for every case the page lists: a plain skill
    gets both links, while a skill named after its app, the reserved ``auto`` and a
    name a real directory already holds get the namespaced link only. The ``~/`` half
    is driven through ``expand_skill_uri``, which must expand against the home and
    ignore ``KIROCREW_HOME``. The mapping half is driven through the loader's
    ``_scoped_entries``, the selection a session's skill block is built from.
    """
    import json

    from kiro_crew import platform_compat
    from kiro_crew.agent_discovery import expand_skill_uri
    from kiro_crew.apps import execution as execution_mod
    from kiro_crew.apps.bridges import _register_skills
    from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
    from kiro_crew.apps.manifest import AppManifest
    from kiro_crew.skill_runtime import catalog as catalog_mod
    from kiro_crew.skills import SkillsLoader

    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(execution_mod, "third_party_execution_allowed", lambda: True)
    skills_root = home / "skills"
    (skills_root / "taken").mkdir(parents=True)
    (skills_root / "taken" / "keep.txt").write_text("mine", encoding="utf-8")

    src = tmp_path / "source" / "design-critique"
    names = ["design-critique", "auto", "taken", "plain", "a-first"]
    for name in names:
        (src / "skills" / name).mkdir(parents=True)
        (src / "skills" / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: d\n---\n# {name}\n", encoding="utf-8"
        )
    manifest = {
        "name": "design-critique",
        "version": "1.0.0",
        "displayName": "Design Critique",
        "description": "d",
        "author": "t",
        "skills": [f"skills/{n}" for n in names],
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    install_app(src)
    app_root = home / "apps" / "design-critique"
    registered = _register_skills(
        "design-critique", AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME), app_root
    )

    assert sorted(registered) == sorted(f"design-critique/{n}" for n in names)
    for name in names:
        assert (skills_root / "design-critique" / name / "SKILL.md").is_file(), name
    assert platform_compat.is_link_or_junction(skills_root / "plain")
    assert platform_compat.is_link_or_junction(skills_root / "a-first")
    assert not (skills_root / "auto").exists()
    assert not platform_compat.is_link_or_junction(skills_root / "taken")
    assert (skills_root / "taken" / "keep.txt").is_file()
    assert not platform_compat.is_link_or_junction(skills_root / "design-critique")
    assert not (
        skills_root / "design-critique" / "SKILL.md"
    ).exists(), "a flat skills/<skill>/SKILL.md exists for a skill named after its app"

    uri = "skill://~/.kiro/crew/skills/design-critique/plain/SKILL.md"
    assert expand_skill_uri(uri, tmp_path / "a.json") == str(
        Path.home() / ".kiro/crew/skills/design-critique/plain/SKILL.md"
    )

    # Per-skill entries select every skill. The app-wide wildcard drops ``a-first``,
    # whose flat link sorts before the app's directory and is the path the catalog
    # keeps. The second assertion pins that wrong answer, which the page warns about.
    monkeypatch.setattr(catalog_mod, "_disabled_app_names", lambda: frozenset())
    loader = opened(SkillsLoader(skills_path=skills_root, install_builtins=False))
    walked = loader._request_catalog_refresh("")
    assert walked is not None and walked.wait(30)
    per_skill = [str(skills_root / "design-critique" / n / "SKILL.md") for n in names]
    wildcard = [str(skills_root / "design-critique" / "*" / "SKILL.md")]

    def selected(only: list[str]) -> list[str]:
        return sorted(Path(e[1]).parent.name for e in loader._scoped_entries(None, only))

    assert selected(per_skill) == sorted(names)
    assert selected(wildcard) == sorted(set(names) - {"a-first"})

    paragraph = doc_text.split("To map an installed app's skills", 1)[1]
    paragraph = paragraph.split("`file://<glob>`", 1)[0]
    for claim in (
        "`skill://~/.kiro/crew/skills/<app>/<skill>/SKILL.md`",
        "Do not map an app with the wildcard\n`<app>/*/SKILL.md` yet",
        "left out with no error",
        "home, not `KIROCREW_HOME`",
        "same name as its app",
        "reserved\n(`auto`)",
        "real directory of that name already exists",
        "Never map an app skill by its flat path.",
    ):
        assert claim in paragraph, f"§Resources app-skill rule lost {claim!r}"
