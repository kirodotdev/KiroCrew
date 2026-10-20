"""A ``skill://`` mapping must survive another root's copy winning its key.

Package installers copy a dependency's skills into each consumer's own skills
tree, so one key lands under several catalog roots, often at different
revisions. First-wins precedence keeps one row per key, and the mapping named
the copy that lost. The agent is served the file its mapping names.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.skills import SkillsLoader

_BODY = "---\nname: implementation\ndescription: implement code\n---\n# implementation\nBODY\n"


def _write(root: Path, body: str = _BODY) -> Path:
    path = root / "code" / "implementation" / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _loader(tmp_path: Path, roots: list[Path], opened) -> SkillsLoader:
    cfg = KiroCrewConfig()
    cfg.skills.extra_paths = [str(root) for root in roots]
    return opened(
        SkillsLoader(skills_path=tmp_path / "builtin-skills", install_builtins=False, config=cfg)
    )


def test_a_mapping_to_a_shadowed_copy_serves_the_mapped_file(tmp_path, opened):
    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    _write(winner, _BODY.replace("BODY", "UPSTREAM").replace("implement code", "upstream copy"))
    mapped = _write(
        loser, _BODY.replace("BODY", "CONSUMER").replace("implement code", "mapped copy")
    )
    loader = _loader(tmp_path, [winner, loser], opened)

    rows = loader.scoped_skills(only=[str(mapped)])

    assert [(row["key"], row["path"]) for row in rows] == [("code/implementation", str(mapped))]
    # Metadata and body both come from the mapped copy, through its own root.
    assert rows[0]["description"] == "mapped copy"
    assert rows[0]["mapping_root"] == str(loser.resolve())
    body = loader.read_scoped_skill("code/implementation", only=[str(mapped)]) or ""
    assert "CONSUMER" in body and "UPSTREAM" not in body


def test_a_wildcard_mapping_to_a_shadowed_copy_serves_the_mapped_file(tmp_path, opened):
    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    _write(winner, _BODY.replace("BODY", "UPSTREAM"))
    mapped = _write(loser, _BODY.replace("BODY", "CONSUMER"))
    loader = _loader(tmp_path, [winner, loser], opened)

    only = [str(loser / "**" / "SKILL.md")]
    rows = loader.scoped_skills(only=only)

    assert [(row["key"], row["path"]) for row in rows] == [("code/implementation", str(mapped))]
    body = loader.read_scoped_skill("code/implementation", only=only) or ""
    assert "CONSUMER" in body and "UPSTREAM" not in body


def test_an_unscoped_catalog_keeps_first_wins_precedence(tmp_path, opened):
    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    served = _write(winner, _BODY.replace("BODY", "UPSTREAM"))
    _write(loser, _BODY.replace("BODY", "CONSUMER"))
    loader = _loader(tmp_path, [winner, loser], opened)

    rows = [row for row in loader.scoped_skills(only=None) if row["key"] == "code/implementation"]

    assert [row["path"] for row in rows] == [str(served)]


def test_a_mapped_copy_owned_by_a_disabled_app_is_not_served(tmp_path, opened, monkeypatch):
    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    _write(winner, _BODY.replace("BODY", "UPSTREAM"))
    mapped = _write(loser, _BODY.replace("BODY", "CONSUMER"))
    loader = _loader(tmp_path, [winner, loser], opened)
    monkeypatch.setattr(
        loader,
        "_owning_app",
        lambda name, skill_file, within=None: (
            "blocked" if Path(skill_file).is_relative_to(loser) else None
        ),
    )
    monkeypatch.setattr(loader, "_get_disabled_app_names", lambda: frozenset({"blocked"}))

    assert loader.scoped_skills(only=[str(mapped)]) == []
    assert loader.read_scoped_skill("code/implementation", only=[str(mapped)]) is None


def test_a_mapped_copy_the_path_screen_refuses_is_not_served(tmp_path, opened, monkeypatch):
    from kiro_crew import skills as sk

    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    _write(winner, _BODY.replace("BODY", "UPSTREAM"))
    mapped = _write(loser, _BODY.replace("BODY", "CONSUMER"))
    loader = _loader(tmp_path, [winner, loser], opened)
    screen = sk.validate_file_path
    # Stands in for a link to a UNC share, which only a Windows host can build.
    monkeypatch.setattr(
        sk, "validate_file_path", lambda raw: None if Path(raw) == mapped else screen(raw)
    )

    assert loader._scoped_entries(None, [str(mapped)]) == []
    assert loader.read_scoped_skill("code/implementation", only=[str(mapped)]) is None


def test_the_disabled_app_filter_keeps_a_confined_row_confined(tmp_path, opened, monkeypatch):
    from kiro_crew.skill_runtime import catalog

    root = tmp_path / "upstream" / "skills"
    served = _write(root)
    loader = _loader(tmp_path, [root], opened)
    confine = str(tmp_path / "project" / ".kiro" / "skills")
    rows = [("code/implementation", served, confine)]
    monkeypatch.setattr(catalog, "_iter_rows", lambda _loader, _project: rows)
    monkeypatch.setattr(loader, "_get_disabled_app_names", lambda: frozenset({"other"}))
    seen = []
    monkeypatch.setattr(
        loader, "_owning_app", lambda name, skill_file, within=None: seen.append(within)
    )

    loader._scoped_entries(None, [str(served)])

    # Without *within* the owner lookup reads a repository file unconfined.
    assert seen == [confine]


def test_a_shadowed_mapped_copy_does_not_trigger_the_winning_copy(tmp_path, opened):
    from kiro_crew.context_assembly.turn_message import _mapped_trigger_keys

    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    upstream = _write(winner, _BODY.replace("BODY", "UPSTREAM"))
    mapped = _write(loser, _BODY.replace("BODY", "CONSUMER"))
    loader = _loader(tmp_path, [winner, loser], opened)

    # Triggers load the winning copy by key, so only a mapping to it may trigger.
    assert _mapped_trigger_keys(loader, None, [str(mapped)]) == set()
    assert _mapped_trigger_keys(loader, None, [str(upstream)]) == {"code/implementation"}


def test_scopes_mapping_different_copies_share_the_body_index(tmp_path, opened, monkeypatch):
    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    upstream = _write(winner, _BODY.replace("BODY", "upstreamword"))
    mapped = _write(loser, _BODY.replace("BODY", "consumerword"))
    loader = _loader(tmp_path, [winner, loser], opened)
    assert loader._search_index is not None
    reads = []
    read_terms = loader._search_index._read_terms

    def counted(path, **kw):
        reads.append(str(path))
        return read_terms(path, **kw)

    monkeypatch.setattr(loader._search_index, "_read_terms", counted)

    def hits(word: str, only: list[str]) -> list[str]:
        return [row["key"] for row in loader.search_skills(word, only=only)]

    for _ in range(2):
        assert hits("consumerword", [str(mapped)]) == ["code/implementation"]
        assert hits("upstreamword", [str(mapped)]) == []
        assert hits("upstreamword", [str(upstream)]) == ["code/implementation"]
        assert hits("consumerword", [str(upstream)]) == []

    # Alternating scopes never re-index a copy the other scope indexed.
    assert len(reads) == len(set(reads))
    assert set(reads) <= {str(mapped), str(upstream)}


def test_a_losing_copy_outside_every_admitted_root_is_not_served(tmp_path, opened, monkeypatch):
    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    _write(winner, _BODY.replace("BODY", "UPSTREAM"))
    mapped = _write(loser, _BODY.replace("BODY", "CONSUMER"))
    loader = _loader(tmp_path, [winner, loser], opened)
    # The walk already refuses a link out of its roots, so the fence is
    # exercised by admitting only the winner's root.
    monkeypatch.setattr(loader, "_snapshot_admitted_roots", lambda: (str(winner.resolve()),))

    assert loader._scoped_entries(None, [str(mapped)]) == []


def test_a_restricted_legacy_context_does_not_always_load_the_winning_copy(tmp_path, opened):
    pinned = _BODY.replace(
        "description: implement code", "description: implement code\nalways: true"
    )
    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    upstream = _write(winner, pinned.replace("BODY", "UPSTREAM"))
    mapped = _write(loser, pinned.replace("BODY", "CONSUMER"))
    loader = _loader(tmp_path, [winner, loser], opened)

    # Legacy always-loading reads by key, which is the winning copy, so the
    # mapped copy's body is left out rather than replaced.
    context = loader.get_context(only=[str(mapped)])
    assert "UPSTREAM" not in context and "CONSUMER" not in context
    assert "UPSTREAM" in loader.get_context(only=[str(upstream)])


def test_a_mapped_copy_behind_a_disabled_winner_is_read_by_path(tmp_path, opened, monkeypatch):
    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    _write(winner, _BODY.replace("BODY", "UPSTREAM"))
    mapped = _write(loser, _BODY.replace("BODY", "CONSUMER"))
    loader = _loader(tmp_path, [winner, loser], opened)
    monkeypatch.setattr(
        loader,
        "_owning_app",
        lambda name, skill_file, within=None: (
            "blocked" if Path(skill_file).is_relative_to(winner) else None
        ),
    )
    monkeypatch.setattr(loader, "_get_disabled_app_names", lambda: frozenset({"blocked"}))

    # A key read still resolves to the disabled winner, so the mapped copy
    # must keep its own root rather than be read by key.
    [row] = loader.scoped_skills(only=[str(mapped)])
    assert row["mapping_root"] == str(loser.resolve())
    body = loader.read_scoped_skill("code/implementation", only=[str(mapped)]) or ""
    assert "CONSUMER" in body and "UPSTREAM" not in body


def test_a_read_of_a_mapped_losing_copy_is_credited_to_its_key(tmp_path, opened):
    from kiro_crew.skill_runtime.read_credit import _served_key_by_realpath

    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    _write(winner, _BODY.replace("BODY", "UPSTREAM"))
    mapped = _write(loser, _BODY.replace("BODY", "CONSUMER"))
    loader = _loader(tmp_path, [winner, loser], opened)

    assert _served_key_by_realpath(loader)[str(mapped.resolve())] == "code/implementation"


def test_a_root_listed_twice_yields_one_row_per_copy(tmp_path, opened):
    from kiro_crew.skill_runtime import catalog

    root = tmp_path / "upstream" / "skills"
    _write(root)
    loader = _loader(tmp_path, [root, root], opened)

    assert [row[0] for row in catalog._iter_rows(loader)] == ["code/implementation"]


def test_a_copy_linked_into_the_skills_dir_yields_one_row(tmp_path, opened, monkeypatch):
    from kiro_crew import skills_discovery
    from kiro_crew.skill_runtime import catalog

    root = tmp_path / "upstream" / "skills"
    _write(root)
    link = tmp_path / "builtin-skills" / "code" / "implementation"
    link.parent.mkdir(parents=True)
    link.symlink_to(root / "code" / "implementation", target_is_directory=True)
    monkeypatch.setattr(skills_discovery, "_trusted_skill_roots", lambda: (str(root.resolve()),))
    loader = _loader(tmp_path, [root], opened)

    assert [row[0] for row in catalog._iter_rows(loader)] == ["code/implementation"]


def test_a_losing_copy_is_fenced_to_the_narrowest_admitted_root(tmp_path, opened, monkeypatch):
    winner = tmp_path / "upstream" / "skills"
    loser = tmp_path / "consumer" / "skills"
    _write(winner, _BODY.replace("BODY", "UPSTREAM"))
    mapped = _write(loser, _BODY.replace("BODY", "CONSUMER"))
    loader = _loader(tmp_path, [winner, loser], opened)
    outer = str((tmp_path / "consumer").resolve())
    monkeypatch.setattr(loader, "_snapshot_admitted_roots", lambda: (outer, str(loser.resolve())))

    [entry] = loader._scoped_entries(None, [str(mapped)])

    assert entry.mapping_root == str(loser.resolve())
