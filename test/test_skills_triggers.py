"""Which skills a message triggers (``kiro_crew.skills``).

This file pins fuzzy and word-overlap trigger matching, negative triggers and the
``max_triggered`` cap, what a trigger scan costs per message and audits, and the
``repo_scope`` gate. The family's other files are described in ``test_skills.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from skills_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _close_skills_loaders,
    _create_skill,
    _isolate_extra_paths,
    _settled,
)

from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.skills import SkillsLoader


class TestRepoScope:
    """``repo_scope:`` frontmatter mechanically suppresses a skill unless the
    SESSION's active project directory (or an ancestor) contains the named
    relative path — the loader-enforced gate for repo-specific skills with
    destructive instructions (prose scope guards are
    probabilistic; containment must be mechanical before shipping to every
    install).

    The gate is keyed on the project, never on the process working directory:
    this runs in the gateway while it assembles context, so ``Path.cwd()`` is
    the gateway's own directory and answers by install shape rather than by the
    work the session is doing."""

    def _write_skill(self, root: Path, name: str, scope: str | None, always: bool = False) -> None:
        d = root / name
        d.mkdir(parents=True)
        fm = f"---\nname: {name}\ntriggers: zebra quokka\n"
        if scope:
            fm += f"repo_scope: {scope}\n"
        if always:
            fm += "always: true\n"
        (d / "SKILL.md").write_text(fm + "---\nbody")

    def _repo(self, tmp_path: Path, name: str = "checkout") -> Path:
        repo = tmp_path / name
        (repo / "src" / "kiro_crew").mkdir(parents=True)
        # A real checkout carries a .git entry, and the gate reads it as the
        # boundary the ancestor walk stops at, so the fixture needs one to model a
        # repository rather than a bare directory tree.
        (repo / ".git").mkdir()
        return repo

    def _other_repo(self, tmp_path: Path, name: str) -> Path:
        """A project that is some OTHER repository, wherever ``tmp_path`` lives.

        The gate walks a project's ancestors up to the first ``.git``. A bare
        directory under ``tmp_path`` has no boundary of its own, so whether the
        walk finds ``src/kiro_crew`` above it is decided by the host: a harness that
        pins ``TMPDIR`` under this checkout puts the real one in every ancestor
        chain. Giving the project its own ``.git`` models what "outside the repo"
        means to the gate -- a different repository -- and stops the walk there.
        """
        other = tmp_path / name
        other.mkdir()
        (other / ".git").mkdir()
        return other

    def test_scoped_skill_suppressed_without_a_project(self, tmp_path: Path) -> None:
        # No project named at all -> fail closed. An un-scoped surface (eval
        # harness, a session with no project set) never inherits repo rules.
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        loader = SkillsLoader(skills_path=skills, install_builtins=False)
        assert loader.get_triggered_skills("zebra quokka") == []

    def test_scoped_skill_suppressed_outside_repo(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        loader = SkillsLoader(skills_path=skills, install_builtins=False)
        outside = self._other_repo(tmp_path, "elsewhere")
        assert loader.get_triggered_skills("zebra quokka", project_dir=str(outside)) == []

    def test_scoped_skill_eligible_inside_repo(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills, install_builtins=False, config=cfg))
        subdir = self._repo(tmp_path) / "website"
        subdir.mkdir()
        # ancestor of the project dir contains src/kiro_crew
        assert loader.get_triggered_skills("zebra quokka", project_dir=str(subdir)) == ["repo-only"]

    def test_process_cwd_does_not_admit_the_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The regression this gate had: a gateway whose OWN cwd sits inside a
        # scoped checkout admitted the skill into every session, whatever the
        # session was working on. Standing in the repo must decide nothing.
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills, install_builtins=False, config=cfg))
        monkeypatch.chdir(self._repo(tmp_path))
        other = self._other_repo(tmp_path, "some-rust-project")
        assert loader.get_triggered_skills("zebra quokka") == []
        assert loader.get_triggered_skills("zebra quokka", project_dir=str(other)) == []

    def test_always_path_is_gated_identically(self, tmp_path: Path) -> None:
        # Both injection paths share one helper; a pinned skill must not be a
        # way around the gate.
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew", always=True)
        loader = _settled(SkillsLoader(skills_path=skills, install_builtins=False))
        other = self._other_repo(tmp_path, "some-rust-project")
        assert loader.get_always_skills() == []
        assert loader.get_always_skills(str(other)) == []
        assert loader.get_always_skills(str(self._repo(tmp_path))) == ["repo-only"]

    def test_unscoped_skill_unaffected(self, tmp_path: Path) -> None:
        skills = tmp_path / "skills"
        self._write_skill(skills, "anywhere", None)
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills, install_builtins=False, config=cfg))
        assert loader.get_triggered_skills("zebra quokka") == ["anywhere"]

    def test_traversal_scope_fails_closed(self, tmp_path: Path) -> None:
        # ".." in the scope must never widen the check — fails closed.
        skills = tmp_path / "skills"
        self._write_skill(skills, "sneaky", "../..")
        loader = SkillsLoader(skills_path=skills, install_builtins=False)
        assert loader.get_triggered_skills("zebra quokka", project_dir=str(tmp_path)) == []

    def test_scoped_skill_is_absent_from_the_index_too(self, tmp_path: Path) -> None:
        # Dropping only the BODY leaves the summary line in the skill index, and
        # the index tells the agent to read the full file for anything related -
        # so the repo-specific procedure stays one `cat` away.
        skills = tmp_path / "skills"
        self._write_skill(skills, "repo-only", "src/kiro_crew")
        self._write_skill(skills, "anywhere", None)
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills, install_builtins=False, config=cfg))
        other = self._other_repo(tmp_path, "some-rust-project")
        for block in (
            loader.get_context(project_dir=str(other)),
            loader.get_context(budget=100_000, project_dir=str(other)),
            loader.get_context(),
        ):
            assert "repo-only" not in block
            assert "anywhere" in block
        # ...and present once the project is the scoped tree.
        inside = loader.get_context(project_dir=str(self._repo(tmp_path)))
        assert "repo-only" in inside

    def test_kiro_crew_dev_skills_load_globally_and_say_so(self) -> None:
        # Maintainer decision: the Kiro Crew developer skills load in
        # every session like any other skill, so a Kiro Crew PR written from any
        # folder still gets them. The description is what keeps them out of other
        # repositories, so each one must state that it is for this repository.
        from kiro_crew import skills as skills_mod

        pkg = Path(skills_mod.__file__).parent
        expected = {
            pkg
            / "apps"
            / "builtins"
            / "dev_fleet"
            / "skills"
            / "pod-e2e": "ONLY for developing Kiro Crew itself",
            pkg / "builtin_skills" / "kirocrew-dev" / "kirocrew-prepare-pr": "Kiro Crew repo only",
            pkg
            / "builtin_skills"
            / "kirocrew-dev"
            / "kirocrew-worktree-dev": "Kiro Crew source repo ITSELF",
            pkg / "builtin_skills" / "kirocrew-dev" / "writing-tests": "Kiro Crew repo only",
            pkg / "builtin_skills" / "kirocrew-dev" / "dashboard-template": "Kiro Crew repo only",
        }
        for skill_dir, statement in expected.items():
            head = (skill_dir / "SKILL.md").read_text(encoding="utf-8")[:2048]
            frontmatter = head.split("---", 2)[1]
            assert "repo_scope" not in frontmatter, skill_dir.name
            assert statement in frontmatter, skill_dir.name


class TestTriggeredSkills:
    """Tests for fuzzy trigger matching."""

    def _loader_with_skill(self, tmp_path, triggers, monkeypatch=None):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            f"---\nname: tiny-url\ndescription: Shorten URLs\ntriggers: {triggers}\n---\n# Tiny URL\n",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))
        if monkeypatch is not None:
            from unittest.mock import MagicMock

            monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())
        return loader

    def test_exact_trigger_match(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "tiny url, shorten url", monkeypatch)
        assert "tiny-url" in loader.get_triggered_skills("make a tiny url for me")

    def test_reworded_query_matches(self, tmp_path, monkeypatch):
        """Words present but not contiguous — should still match."""
        loader = self._loader_with_skill(tmp_path, "shorten url", monkeypatch)
        assert "tiny-url" in loader.get_triggered_skills("can you shorten this url please")

    def test_fuzzy_partial_overlap(self, tmp_path, monkeypatch):
        """≥70% word overlap triggers the skill."""
        loader = self._loader_with_skill(tmp_path, "shorten this url", monkeypatch)
        # 2 of 3 words = 66% → no match
        assert "tiny-url" not in loader.get_triggered_skills("shorten this link")
        # 3 of 3 words = 100% → match
        assert "tiny-url" in loader.get_triggered_skills("shorten this url now")

    def test_no_match_unrelated(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "tiny url, shorten url", monkeypatch)
        assert loader.get_triggered_skills("check my pipeline health") == []

    def test_always_skills_excluded(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "always-on",
            "---\nname: always-on\nalways: true\ntriggers: hello world\n---\n# Always\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        from unittest.mock import MagicMock

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())
        assert loader.get_triggered_skills("hello world") == []

    def test_case_insensitive(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "Tiny URL", monkeypatch)
        assert "tiny-url" in loader.get_triggered_skills("Make a TINY url")

    def test_multiple_triggers_first_wins(self, tmp_path, monkeypatch):
        loader = self._loader_with_skill(tmp_path, "tiny url, shorten url", monkeypatch)
        result = loader.get_triggered_skills("shorten this url for me")
        assert result.count("tiny-url") == 1

    def test_empty_triggers_no_crash(self, tmp_path, monkeypatch):
        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "no-trigger",
            "---\nname: no-trigger\ndescription: No triggers\n---\n# No Trigger\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)
        from unittest.mock import MagicMock

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())
        assert loader.get_triggered_skills("anything") == []

    def test_trigger_match_does_not_record_usage(self, tmp_path, monkeypatch):
        """A trigger match must not earn ranking weight in the ledger.

        Only body delivery (the context builder calling _record_use after
        load_skill succeeds) should update the hotness ranking. False-positive
        trigger matches must not drift the top-K toward skills the agent never
        reads.
        """
        loader = self._loader_with_skill(tmp_path, "tiny url", monkeypatch)

        # Confirm the skill triggers on the message
        triggered = loader.get_triggered_skills("make a tiny url for me")
        assert "tiny-url" in triggered

        # The ledger must still be at zero: a match alone is not a delivery
        assert loader._usage is not None
        assert loader._usage.score("tiny-url")[0] == 0.0

        # Simulating body delivery (what context.py does after load_skill)
        # must update the ledger
        loader._record_use("tiny-url")
        assert loader._usage.score("tiny-url")[0] == 1.0


class TestTriggerMatching:
    """Tests for word-overlap matching with negative keywords and max_triggered."""

    def test_basic_trigger_match(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: Get weather info\ntriggers: weather forecast\n---\n",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("what's the weather forecast today")
        assert "weather" in result

    def test_negative_trigger_excludes(self, tmp_path, monkeypatch):
        """Negative trigger !keyword should exclude the skill."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "code-search",
            "---\nname: code-search\ndescription: Search code\ntriggers: search code, !search examples\n---\n",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        # Positive match without negative words
        assert "code-search" in loader.get_triggered_skills("search code repositories")
        # Negative trigger fires — "search" and "examples" both present
        assert "code-search" not in loader.get_triggered_skills("search for code examples")

    def test_max_triggered_limit(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        for i in range(5):
            _create_skill(
                skills_dir,
                f"skill{i}",
                f"---\nname: skill{i}\ndescription: Skill {i}\ntriggers: test\n---\n",
            )
        # max_triggered is snapshotted at construction, so inject via config=.
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=2))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("test")
        assert len(result) == 2

    def test_sort_by_overlap_score(self, tmp_path, monkeypatch):
        """Higher overlap skills should appear first."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        # "good" trigger "alpha beta gamma delta" → 3/4 = 0.75 (above 0.7)
        _create_skill(
            skills_dir,
            "good",
            "---\nname: good\ndescription: Good\ntriggers: alpha beta gamma delta\n---\n",
        )
        # "better" trigger "alpha beta gamma" → 3/3 = 1.0
        _create_skill(
            skills_dir,
            "better",
            "---\nname: better\ndescription: Better\ntriggers: alpha beta gamma\n---\n",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=5))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("alpha beta gamma")
        assert len(result) == 2
        assert result[0] == "better"  # higher overlap first
        assert result[1] == "good"

    def test_always_on_skills_skipped(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir, "always", "---\nname: always\nalways: true\ntriggers: test\n---\n"
        )
        _create_skill(skills_dir, "normal", "---\nname: normal\ntriggers: test\n---\n")
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=5))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        result = loader.get_triggered_skills("test")
        assert "always" not in result
        assert "normal" in result

    def test_multi_word_trigger_phrase(self, tmp_path, monkeypatch):
        """Multi-word trigger phrases should match as a unit."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: Shorten URLs\ntriggers: shorten url, create tiny link, make short url\n---\n",
        )
        cfg = KiroCrewConfig(skills=SkillsConfig(max_triggered=3))
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))

        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        assert "tiny-url" in loader.get_triggered_skills("please shorten this url")
        assert "tiny-url" in loader.get_triggered_skills("create a tiny link for me")
        # Single word "url" alone shouldn't trigger (1/2 = 50% < 70%)
        assert "tiny-url" not in loader.get_triggered_skills("what is a url")


class TestTriggerPerformance:
    """Per-message cost reductions in get_triggered_skills (perf)."""

    def test_single_audit_event_for_triggered_set(self, tmp_path, monkeypatch):
        """One SEL event per call (for the matched set), not one per skill."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        # Several skills; only one will match the query.
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        _create_skill(
            skills_dir,
            "weather",
            "---\nname: weather\ndescription: d\ntriggers: weather forecast\n---\n# x\n",
        )
        _create_skill(
            skills_dir,
            "pipeline",
            "---\nname: pipeline\ndescription: d\ntriggers: pipeline health\n---\n# x\n",
        )
        loader = _settled(
            SkillsLoader(
                skills_path=skills_dir,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)
        triggered = loader.get_triggered_skills("please shorten this url")

        assert "tiny-url" in triggered
        # Exactly ONE audit event, regardless of how many skills were scanned.
        assert fake_sel.log_tool_invocation.call_count == 1

    def test_no_audit_event_when_nothing_triggers(self, tmp_path, monkeypatch):
        """The common case (no match) writes zero SEL events."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        loader = SkillsLoader(skills_path=skills_dir, install_builtins=False)

        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)
        assert loader.get_triggered_skills("hello there friend") == []
        assert fake_sel.log_tool_invocation.call_count == 0

    @pytest.mark.parametrize(
        "triggers",
        [
            "shorten url, !test",  # positive first
            "!test, shorten url",  # negative first — must be order-independent
        ],
    )
    def test_negative_trigger_exclusion_is_audited(self, tmp_path, monkeypatch, triggers):
        """A negative trigger that excludes an otherwise-matching skill is a
        permission DENY and must still emit an audit event (with the negated
        skill in metadata), regardless of trigger order in the frontmatter."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            f"---\nname: tiny-url\ndescription: d\ntriggers: {triggers}\n---\n# x\n",
        )
        # A positive cap: the denial is a DENY only where the match could have
        # been a grant. At the shipped cap of 0 the same veto is not audited
        # (see test_zero_cap_writes_no_denied_audit_row).
        loader = _settled(
            SkillsLoader(
                skills_path=skills_dir,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )

        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)
        # "shorten url" matches the positive trigger, but "test" fires the
        # negative trigger → excluded.
        triggered = loader.get_triggered_skills("shorten url for this test")

        assert triggered == []  # excluded by negative trigger
        # The denial is audited (one event), with the skill named in metadata —
        # even when "!test" is listed before "shorten url".
        assert fake_sel.log_tool_invocation.call_count == 1
        _, kwargs = fake_sel.log_tool_invocation.call_args
        assert kwargs["outcome"] == "denied"
        assert "tiny-url" in kwargs["metadata"]["negated"]

    def test_iter_result_is_cached(self, tmp_path, monkeypatch):
        """The skill-file walk is cached, not re-run on every call."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        # A positive cap so the scan actually runs; at 0 there is no walk to cache.
        loader = SkillsLoader(
            skills_path=skills_dir,
            install_builtins=False,
            config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
        )
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        calls = {"n": 0}
        orig = loader._iter_uncached

        def _counting(project_key=None):
            calls["n"] += 1
            return orig(project_key)

        monkeypatch.setattr(loader, "_iter_uncached", _counting)
        # Settled only once the counter is in place, so the first walk is counted:
        # exactly one, and the five messages after it must add none.
        _settled(loader)
        for _ in range(5):
            loader.get_triggered_skills("shorten this url")
        assert calls["n"] == 1

    def test_config_not_loaded_per_message(self, tmp_path, monkeypatch):
        """get_triggered_skills must not re-load config on every message — the
        trigger cap is snapshotted at construction (max_triggered hoist)."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        loader = _settled(
            SkillsLoader(
                skills_path=skills_dir,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
            )
        )
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        calls = {"n": 0}
        real_load = KiroCrewConfig.load

        def _counting_load():
            calls["n"] += 1
            return real_load()

        monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", _counting_load)
        for _ in range(5):
            loader.get_triggered_skills("shorten this url")
        # Zero config loads across 5 messages — the cap was snapshotted in __init__.
        assert calls["n"] == 0

    def test_max_triggered_snapshot_caps_results(self, tmp_path, monkeypatch):
        """The snapshotted max_triggered still bounds the result count."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        for i in range(5):
            _create_skill(
                skills_dir,
                f"s{i}",
                f"---\nname: s{i}\ndescription: d\ntriggers: shorten url\n---\n# x\n",
            )
        cfg = KiroCrewConfig()
        cfg.skills.max_triggered = 2
        loader = _settled(SkillsLoader(skills_path=skills_dir, install_builtins=False, config=cfg))
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        triggered = loader.get_triggered_skills("shorten url")
        assert len(triggered) == 2

    def test_zero_cap_skips_the_scan_entirely(self, tmp_path, monkeypatch):
        """At the shipped default (``max_triggered = 0``) the matcher is off: no
        skill is walked, read or scored, because every score would be sliced away.
        The per-message cost is one cap read, and the result is the same ``[]``."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        loader = SkillsLoader(
            skills_path=skills_dir,
            install_builtins=False,
            config=KiroCrewConfig(skills=SkillsConfig(max_triggered=0)),
        )
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)
        walk = MagicMock(return_value=[])
        monkeypatch.setattr(loader, "_iter_visible", walk)
        read = MagicMock(return_value={})
        monkeypatch.setattr(loader, "_cached_frontmatter", read)

        assert loader.get_triggered_skills("shorten this url") == []
        walk.assert_not_called()
        read.assert_not_called()
        assert fake_sel.log_tool_invocation.call_count == 0

    def test_zero_cap_writes_no_denied_audit_row(self, tmp_path, monkeypatch):
        """A ``!`` veto at cap 0 excludes nothing that could have been injected, so
        it is not a permission DENY and writes no ``skill_trigger`` row -- unlike
        the same veto under a positive cap (test_negative_trigger_exclusion_is_audited)."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url, !test\n---\n# x\n",
        )
        loader = SkillsLoader(
            skills_path=skills_dir,
            install_builtins=False,
            config=KiroCrewConfig(skills=SkillsConfig(max_triggered=0)),
        )
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)

        assert loader.get_triggered_skills("shorten url for this test") == []
        assert fake_sel.log_tool_invocation.call_count == 0

    def test_zero_cap_still_consults_select(self, tmp_path, monkeypatch):
        """The zero-cap skip belongs to the matcher, not to ``select``: a
        selection point still runs, owns its own zero-cap refusal, and a pick it
        returns is injected and audited as a selection."""
        from unittest.mock import MagicMock

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        loader = _settled(
            SkillsLoader(
                skills_path=skills_dir,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=0)),
            )
        )
        fake_sel = MagicMock()
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: fake_sel)
        select = MagicMock(return_value=["tiny-url"])

        assert loader.get_triggered_skills("hello there friend", select=select) == ["tiny-url"]
        select.assert_called_once_with()
        assert fake_sel.log_tool_invocation.call_count == 1
        _, kwargs = fake_sel.log_tool_invocation.call_args
        assert kwargs["outcome"] == "triggered"
        assert kwargs["metadata"]["selected"] == "true"
        assert kwargs["metadata"]["skills"] == "tiny-url"

    def test_zero_cap_is_read_live_so_raising_it_turns_the_scan_back_on(
        self, tmp_path, monkeypatch
    ):
        """The skip reads the cap from the live snapshot, so ``kirocrew config set
        skills.max_triggered 3`` re-enables the scan for the very next message
        without rebuilding the loader."""
        from unittest.mock import MagicMock

        from kiro_crew.config import live

        skills_dir = tmp_path / "skills"
        _create_skill(
            skills_dir,
            "tiny-url",
            "---\nname: tiny-url\ndescription: d\ntriggers: shorten url\n---\n# x\n",
        )
        loader = _settled(
            SkillsLoader(
                skills_path=skills_dir,
                install_builtins=False,
                config=KiroCrewConfig(skills=SkillsConfig(max_triggered=0)),
            )
        )
        monkeypatch.setattr("kiro_crew.skills.sel", lambda: MagicMock())

        assert loader.get_triggered_skills("shorten this url") == []
        cfg = KiroCrewConfig()
        cfg.skills.max_triggered = 3
        live.watch().prime(cfg)
        assert loader.get_triggered_skills("shorten this url") == ["tiny-url"]
