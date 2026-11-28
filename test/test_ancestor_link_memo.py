"""``first_linked_ancestor`` tests each shared ancestor once inside ``ancestor_link_memo``.

The skills listing screens every agent glob for linked ancestors; globs under the same
folders re-tested those folders once per glob. Inside the memo each ancestor is tested
once per block, a link at a shared ancestor is still reported for every path beneath it,
and nothing is kept past the block, so a folder that becomes a link is caught next time.
"""

from __future__ import annotations

import collections
import contextlib
import os
from pathlib import Path

import pytest

from kiro_crew import link_screen_memo, platform_compat, skills
from kiro_crew.dashboard.handlers import _shared_skill_listing
from kiro_crew.link_screen_memo import active_memo, ancestor_link_memo
from kiro_crew.skill_runtime import catalog
from kiro_crew.testing.links import make_dir_link


def _counting(monkeypatch: pytest.MonkeyPatch, links: set[str]) -> list[str]:
    calls: list[str] = []

    def fake(path: str | os.PathLike) -> bool:
        calls.append(str(path))
        return str(path) in links

    monkeypatch.setattr(platform_compat, "is_link_or_junction", fake)
    return calls


def _leaves(base: Path) -> list[Path]:
    return [base / "a" / "b" / f"skill{i}" / "SKILL.md" for i in range(5)]


def test_without_memo_every_call_walks_every_ancestor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _counting(monkeypatch, set())
    leaves = _leaves(tmp_path)
    for leaf in leaves:
        assert platform_compat.first_linked_ancestor(leaf) is None
    assert len(calls) == sum(len(leaf.parents) for leaf in leaves)


def test_memo_tests_each_distinct_ancestor_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _counting(monkeypatch, set())
    leaves = _leaves(tmp_path)
    with ancestor_link_memo():
        for leaf in leaves:
            assert platform_compat.first_linked_ancestor(leaf) is None
    distinct = {str(p) for leaf in leaves for p in leaf.parents}
    assert sorted(calls) == sorted(distinct)


def test_shared_linked_ancestor_is_reported_for_every_path_beneath_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    shared = str(tmp_path / "a")
    calls = _counting(monkeypatch, {shared})
    with ancestor_link_memo():
        for leaf in _leaves(tmp_path):
            assert platform_compat.first_linked_ancestor(leaf) == shared
    assert calls.count(shared) == 1
    # Root-first, stopping at the link: nothing below it is ever tested.
    assert not any(c.startswith(str(tmp_path / "a" / "b")) for c in calls)


def test_an_ancestor_that_becomes_a_link_is_caught_by_the_next_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    links: set[str] = set()
    _counting(monkeypatch, links)
    leaf = _leaves(tmp_path)[0]
    with ancestor_link_memo():
        assert platform_compat.first_linked_ancestor(leaf) is None
    links.add(str(tmp_path / "a"))
    with ancestor_link_memo():
        assert platform_compat.first_linked_ancestor(leaf) == str(tmp_path / "a")
    assert platform_compat.first_linked_ancestor(leaf) == str(tmp_path / "a")


def test_nested_block_shares_the_outer_answers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _counting(monkeypatch, set())
    leaf = _leaves(tmp_path)[0]
    with ancestor_link_memo():
        platform_compat.first_linked_ancestor(leaf)
        with ancestor_link_memo():
            platform_compat.first_linked_ancestor(leaf)
        platform_compat.first_linked_ancestor(leaf)
    assert len(calls) == len(leaf.parents)
    assert active_memo() is None


def test_real_link_at_shared_ancestor_is_found_for_every_glob(tmp_path: Path) -> None:
    (tmp_path / "real" / "b").mkdir(parents=True)
    make_dir_link(tmp_path / "a", tmp_path / "real")
    with ancestor_link_memo():
        for leaf in _leaves(tmp_path):
            assert platform_compat.first_linked_ancestor(leaf) == str(tmp_path / "a")


def _linked_agents(tmp_path: Path, agents: int, per_agent: int) -> list:
    """Agent specs whose distinct skill globs all sit under one linked folder."""
    (tmp_path / "real").mkdir()
    make_dir_link(tmp_path / "linked", tmp_path / "real")
    agent = tmp_path / "agent.json"
    return [
        (
            f"agent{a}",
            {
                "resources": [
                    f"skill://{tmp_path / 'linked' / f's{a}-{i}' / 'SKILL.md'}"
                    for i in range(per_agent)
                ]
            },
            agent,
        )
        for a in range(agents)
    ]


def test_the_real_gate_chain_runs_inside_the_memo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[bool] = []
    real_validate = skills.validate_file_path

    def recording(raw: str, **kwargs):
        seen.append(active_memo() is not None)
        return real_validate(raw, **kwargs)

    monkeypatch.setattr(skills, "validate_file_path", recording)
    expanded = _shared_skill_listing._expand_agent_globs(_linked_agents(tmp_path, 3, 2))
    assert len(seen) == 6 and all(seen)
    assert len(expanded) == 3
    if platform_compat.IS_WINDOWS:
        # The canonical spelling there rests on the junction screen's own rewrite.
        return
    real = os.path.realpath(tmp_path / "real")
    for _name, globs in expanded:
        assert any(g.startswith(real) for g in globs)


def test_a_credentials_leaf_is_still_refused_inside_the_memo() -> None:
    leaf = os.path.join(os.path.expanduser("~"), ".aws", "credentials")
    with ancestor_link_memo():
        assert skills.validate_file_path(leaf, canonical_gate=True) is None
        assert skills.validate_file_path(leaf, canonical_gate=True) is None


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="the link screen runs on Windows only")
def test_windows_annotation_tests_a_shared_ancestor_fewer_times(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parsed = _linked_agents(tmp_path, 10, 5)
    calls: list[str] = []
    real_is_link = platform_compat.is_link_or_junction

    def counting(path: str | os.PathLike) -> bool:
        calls.append(str(path))
        return real_is_link(path)

    monkeypatch.setattr(platform_compat, "is_link_or_junction", counting)
    memoised = _shared_skill_listing._expand_agent_globs(parsed)
    with_memo = calls.count(str(tmp_path))
    calls.clear()
    # The same globs, each canonicalised on its own with no memo open.
    for _name, data, agent in parsed:
        for res in data["resources"]:
            glob = _shared_skill_listing._expand_resource_uri(res, agent)
            _shared_skill_listing._with_canonical_globs([glob])
    without_memo = calls.count(str(tmp_path))
    assert without_memo >= 50
    assert with_memo < without_memo
    assert [n for n, _ in memoised] == [f"agent{a}" for a in range(10)]
    with ancestor_link_memo():
        assert skills.validate_file_path(r"\\host\share\SKILL.md", canonical_gate=True) is None


def test_expand_agent_globs_runs_the_canonicalisation_inside_the_memo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[bool] = []

    def fake_canonical(globs, project_dir=None, cache=None):
        seen.append(active_memo() is not None)
        return globs

    monkeypatch.setattr(_shared_skill_listing, "_with_canonical_globs", fake_canonical)
    agent = tmp_path / "agent.json"
    parsed = [
        (f"agent{i}", {"resources": [f"skill://{tmp_path}/skills/s{i}/SKILL.md"]}, agent)
        for i in range(3)
    ]
    expanded = _shared_skill_listing._expand_agent_globs(parsed)
    assert [name for name, _globs in expanded] == ["agent0", "agent1", "agent2"]
    assert seen == [True, True, True]
    assert active_memo() is None


def test_skill_rows_for_globs_runs_the_canonicalisation_inside_the_memo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[bool] = []

    def fake_canonical(globs, project_dir=None, cache=None):
        seen.append(active_memo() is not None)
        return globs

    monkeypatch.setattr(_shared_skill_listing, "_with_canonical_globs", fake_canonical)
    skill = tmp_path / "skills" / "s0" / "SKILL.md"
    rows = [{"path": str(skill)}, {"path": str(tmp_path / "other" / "SKILL.md")}]
    kept = _shared_skill_listing.skill_rows_for_globs(rows, [str(skill)])
    assert kept == [rows[0]]
    assert seen == [True]
    assert active_memo() is None


def test_annotation_tests_each_distinct_ancestor_once_at_the_issue_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """50 agents x 55 paths over 350 distinct skills: each ancestor the gate screens is
    probed once, the same ones as with no memo, and ``loaded_by_agents`` is unchanged."""
    files = [tmp_path / "skills" / f"s{k:03d}" / "SKILL.md" for k in range(350)]
    for f in files:
        f.parent.mkdir(parents=True)
        f.write_text("---\nname: x\n---\n", encoding="utf-8")
    agent = tmp_path / "agent.json"
    parsed = [
        (
            f"agent{a}",
            {"resources": [f"skill://{files[(a * 7 + i) % 350]}" for i in range(55)]},
            agent,
        )
        for a in range(50)
    ]
    monkeypatch.setattr(_shared_skill_listing, "_load_parsed_agents", lambda: parsed)
    calls = _counting(monkeypatch, set())
    real_validate = skills.validate_file_path

    def screened(raw: str, **kwargs):
        # The Windows gate's ancestor screen, run on every host so the count is exact.
        platform_compat.first_linked_ancestor(raw)
        return real_validate(raw, **kwargs)

    monkeypatch.setattr(skills, "validate_file_path", screened)
    # As on Windows, every glob takes the gate rather than POSIX's canonical skip.
    monkeypatch.setattr(catalog, "_spelled_canonically", lambda *_a: False)

    def annotate() -> list[list[str]]:
        rows: list[dict] = [{"path": str(f)} for f in files]
        _shared_skill_listing.annotate_skills_with_agents(rows)
        return [sorted(r["loaded_by_agents"]) for r in rows]

    def fixture_probes() -> collections.Counter[str]:
        return collections.Counter(c for c in calls if c.startswith(str(tmp_path)))

    memoised = annotate()
    with_memo = fixture_probes()
    # The globs' ancestors inside the fixture: tmp_path, skills/ and each s<k>/ folder.
    distinct = {str(p) for f in files for p in f.parents if str(p).startswith(str(tmp_path))}
    assert with_memo == collections.Counter(distinct)
    assert all(memoised)
    calls.clear()
    monkeypatch.setattr(link_screen_memo, "ancestor_link_memo", contextlib.nullcontext)
    assert annotate() == memoised
    without_memo = fixture_probes()
    assert set(without_memo) == distinct
    assert without_memo[str(tmp_path)] == 350
