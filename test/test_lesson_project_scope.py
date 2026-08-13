"""The project-scope gate, and the lesson paths that apply it.

Covers the shared rule itself, the JSONL store's filter, and that the skill
loader answers through the same function -- the property that keeps skills and
lessons from drifting into two notions of "in scope".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.project_scope import project_scope_satisfied
from kiro_crew.vector_memory import _lesson_display_text, _lesson_scope, _lesson_slug


class TestProjectScopeSatisfied:
    """The shared gate. Fails closed on everything it cannot confirm."""

    def test_project_containing_the_fragment_qualifies(self, tmp_path):
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        assert project_scope_satisfied("src/pkg", tmp_path) is True

    def test_descendant_of_the_project_qualifies(self, tmp_path):
        # The walk up exists so a session working DEEPER in the repository still
        # qualifies. A ".git" entry is what marks the repository, so the fixture
        # carries one -- without it there is no boundary to walk to and the gate
        # only answers for the project dir itself (see the next test).
        (tmp_path / ".git").mkdir()
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        deep = tmp_path / "src" / "pkg" / "sub"
        deep.mkdir()
        assert project_scope_satisfied("src/pkg", deep) is True

    def test_the_walk_stops_at_the_repository_root(self, tmp_path):
        # Above the repository root a fragment can name a shared directory that
        # exists for every project on the host, which would turn a repo-scoped
        # entry global. The walk must not reach there.
        outer = tmp_path / "outer"
        repo = outer / "repo"
        (repo / ".git").mkdir(parents=True)
        (outer / "marker").mkdir()
        assert project_scope_satisfied("marker", repo) is False
        # Same fragment INSIDE the repository still qualifies.
        (repo / "marker").mkdir()
        assert project_scope_satisfied("marker", repo) is True

    def test_a_non_repository_project_answers_only_for_itself(self, tmp_path):
        # No .git anywhere: there is no repository boundary to confirm against, so
        # only the project dir itself is offered rather than an unbounded walk.
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        deep = tmp_path / "src" / "pkg" / "sub"
        deep.mkdir()
        assert project_scope_satisfied("src/pkg", tmp_path) is True
        assert project_scope_satisfied("src/pkg", deep) is False

    def test_absolute_fragment_refused(self, tmp_path):
        # An absolute path is not a fragment. Reducing "/etc/passwd" to
        # "etc/passwd" and matching it against an ancestor is what let a
        # repo-scoped entry apply to every project on the host.
        (tmp_path / ".git").mkdir()
        (tmp_path / "src").mkdir()
        assert project_scope_satisfied("/src", tmp_path) is False
        assert project_scope_satisfied("/etc/passwd", tmp_path) is False

    def test_trailing_slash_ignored(self, tmp_path):
        (tmp_path / ".git").mkdir()
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        assert project_scope_satisfied("src/pkg/", tmp_path) is True

    def test_unrelated_project_does_not_qualify(self, tmp_path):
        other = tmp_path / "elsewhere"
        other.mkdir()
        assert project_scope_satisfied("src/pkg", other) is False

    def test_absent_project_fails_closed(self, tmp_path):
        # The gate cannot confirm the project, so the entry is withheld rather
        # than admitted into a session whose repository is unknown.
        assert project_scope_satisfied("src/pkg", None) is False
        assert project_scope_satisfied("src/pkg", "") is False

    def test_traversal_fragment_refused(self, tmp_path):
        (tmp_path / "src").mkdir()
        assert project_scope_satisfied("../src", tmp_path) is False

    def test_dot_fragment_refused(self, tmp_path):
        # "." is a natural way to write "this repo" and every directory contains it,
        # so accepting it would silently make a scoped entry global -- fail-OPEN.
        assert project_scope_satisfied(".", tmp_path) is False
        assert project_scope_satisfied("./", tmp_path) is False
        assert project_scope_satisfied("src/./pkg", tmp_path) is False
        assert project_scope_satisfied("..", tmp_path) is False

    def test_drive_qualified_fragment_refused(self, tmp_path):
        # pathlib discards the left side of the join when the right is absolute, so
        # on Windows this would answer about a path outside the project entirely.
        # A POSIX leading slash needs no such guard: it is stripped to a relative
        # fragment, so the join stays under the project either way.
        (tmp_path / "etc").mkdir()
        assert project_scope_satisfied("C:/etc", tmp_path) is False
        assert project_scope_satisfied("C:\\etc", tmp_path) is False

    def test_empty_segment_refused(self, tmp_path):
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        assert project_scope_satisfied("src//pkg", tmp_path) is False

    def test_the_schema_refuses_a_scope_the_gate_cannot_satisfy(self):
        # Storing "." reported success and then applied nowhere -- the same
        # silent-success shape this feature exists to end. The write surface now
        # refuses it instead of persisting an inert lesson.
        from kiro_crew.validation import LEARN_ADD_SCHEMA, ValidationError, validate_tool_args

        base = {"rule": "r", "category": "tool"}
        for bad in (".", "..", "src/./pkg", "C:/x"):
            with pytest.raises(ValidationError):
                validate_tool_args({**base, "repo_scope": bad}, LEARN_ADD_SCHEMA)
        cleaned = validate_tool_args({**base, "repo_scope": "src/pkg"}, LEARN_ADD_SCHEMA)
        assert cleaned["repo_scope"] == "src/pkg"

    @requires_symlinks
    def test_a_symlink_out_of_the_repository_is_refused(self, tmp_path):
        # The bounded walk stops the SEARCH from climbing out; strict resolution
        # still followed a link out. A target outside the repository cannot answer
        # "is this fragment inside the repository it names", sensitive or not -- this
        # one deliberately is NOT sensitive, so only containment can refuse it.
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        outside = tmp_path / "outside" / "payload"
        outside.mkdir(parents=True)
        (repo / "data").symlink_to(outside)

        assert project_scope_satisfied("data", repo) is False
        # A target that stays inside still qualifies, so containment has not simply
        # disabled the gate.
        (repo / "src").mkdir()
        assert project_scope_satisfied("src", repo) is True

    @requires_symlinks
    def test_a_symlink_into_a_sensitive_tree_is_refused(self, tmp_path, monkeypatch):
        # The static bypass, no race needed: a symlink already in the tree makes the
        # literal fragment look harmless while the probe follows it into the
        # credential directory. Judging the RESOLVED path is what closes it.
        from kiro_crew import project_scope as ps
        from kiro_crew.security import is_sensitive_path

        # Aim check: the real predicate flags the shape the guard exists for.
        assert is_sensitive_path(str(Path.home() / ".ssh" / "id_rsa")) is True

        (tmp_path / ".git").mkdir()
        secret_dir = tmp_path / "pretend_home" / ".ssh"
        secret_dir.mkdir(parents=True)
        (secret_dir / "id_rsa").write_text("x", encoding="utf-8")
        (tmp_path / "data").symlink_to(secret_dir)

        # Flag the resolved location, as the real predicate would for a true home.
        # Judge by path SEGMENTS, not by a separator: resolve() yields backslashes
        # on Windows, so a hardcoded "/.ssh/" would silently never match there and
        # the assertion below would pass for the wrong reason.
        monkeypatch.setattr(
            ps, "is_sensitive_path", lambda p, base_dir=None: ".ssh" in Path(p).parts
        )
        assert project_scope_satisfied("data/id_rsa", tmp_path) is False
        # A non-sensitive target through the same tree still qualifies.
        (tmp_path / "src").mkdir()
        assert project_scope_satisfied("src", tmp_path) is True

    def test_a_nonexistent_and_a_sensitive_fragment_are_indistinguishable(self, tmp_path):
        # Both answer False, so the gate leaks no existence bit for a path it
        # refuses to look at.
        (tmp_path / ".git").mkdir()
        assert project_scope_satisfied("no/such/thing", tmp_path) is False

    def test_the_write_pattern_and_the_gate_agree(self, tmp_path):
        # The write surface screens the RAW submitted value with SCOPE_FRAGMENT_RE
        # and the gate enforces the rest. If they disagree, a value is either stored
        # and never usable or refused while being perfectly fine -- so pin both
        # surfaces, against the raw value, to the same verdicts.
        from kiro_crew.project_scope import SCOPE_FRAGMENT_RE

        (tmp_path / ".git").mkdir()
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        accept = ["src/pkg", "src/pkg/", "src"]
        reject = [
            ".",
            "..",
            "./",
            "src/./pkg",
            "src/../pkg",
            "src//pkg",
            "C:/x",
            "C:\\x",
            "",
            "/src/pkg",
            "/etc/passwd",
        ]
        for value in accept:
            assert SCOPE_FRAGMENT_RE.match(value), value
            assert project_scope_satisfied(value, tmp_path) is True, value
        for value in reject:
            assert not SCOPE_FRAGMENT_RE.match(value), value
            assert project_scope_satisfied(value, tmp_path) is False, value

    def test_blank_fragment_refused(self, tmp_path):
        assert project_scope_satisfied("", tmp_path) is False
        assert project_scope_satisfied("   ", tmp_path) is False
        assert project_scope_satisfied("/", tmp_path) is False

    def test_a_leading_slash_is_no_longer_tolerated(self, tmp_path):
        # Earlier this reduced "/src/pkg" to "src/pkg" and matched. That tolerance
        # is what made "/etc/passwd" reachable as "etc/passwd" from the "/" ancestor,
        # so an absolute fragment is now refused outright.
        (tmp_path / ".git").mkdir()
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        assert project_scope_satisfied("/src/pkg/", tmp_path) is False
        assert project_scope_satisfied("src/pkg", tmp_path) is True


class TestSkillLoaderSharesTheGate:
    """The skill gate must answer through the shared function, not a copy."""

    def test_skill_gate_delegates_to_the_shared_rule(self, tmp_path, monkeypatch):
        from kiro_crew import skills as skills_mod

        calls: list[tuple[str, object]] = []

        def _spy(relpath, project_dir):
            calls.append((relpath, project_dir))
            return True

        monkeypatch.setattr(skills_mod, "project_scope_satisfied", _spy)
        assert skills_mod.SkillsLoader._repo_scope_satisfied("src/pkg", tmp_path) is True
        # A second implementation in skills.py would leave this empty, which is the
        # drift this test exists to prevent.
        assert calls == [("src/pkg", tmp_path)]


class TestLessonStoreScope:
    """The JSONL store's pre-injection filter."""

    def _store(self, tmp_path):
        return LessonStore(base_dir=tmp_path)

    def test_unscoped_lesson_reaches_every_session(self, tmp_path):
        store = self._store(tmp_path)
        store.save(Lesson(ts="t", rule="always rebase", category="tool"))
        # No project at all: an unscoped lesson is unaffected, which is what an
        # existing store expects.
        assert "always rebase" in store.get_context()
        assert "always rebase" in store.get_context(project_dir=tmp_path)

    def test_scoped_lesson_withheld_outside_its_repo(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "src" / "pkg").mkdir(parents=True)
        outside = tmp_path / "other"
        outside.mkdir()
        store = self._store(tmp_path)
        store.save(Lesson(ts="t", rule="run the repo gate", category="tool", repo_scope="src/pkg"))
        assert "run the repo gate" in store.get_context(project_dir=repo)
        assert "run the repo gate" not in store.get_context(project_dir=outside)

    def test_scoped_lesson_withheld_when_project_unknown(self, tmp_path):
        (tmp_path / "src" / "pkg").mkdir(parents=True)
        store = self._store(tmp_path)
        store.save(Lesson(ts="t", rule="scoped rule", category="tool", repo_scope="src/pkg"))
        # Fail-closed: a surface with no project must not inherit another
        # repository's rules.
        assert store.get_context() == ""

    def test_scope_survives_a_round_trip(self, tmp_path):
        store = self._store(tmp_path)
        store.save(Lesson(ts="t", rule="r", category="tool", repo_scope="src/pkg"))
        assert self._store(tmp_path).load_all()[0].repo_scope == "src/pkg"

    def test_blank_scope_stores_as_absent(self, tmp_path):
        store = self._store(tmp_path)
        store.save(Lesson(ts="t", rule="r", category="tool", repo_scope="   "))
        assert store.load_all()[0].repo_scope is None

    def test_scoping_an_existing_lesson_adds_a_row_rather_than_mutating_it(self, tmp_path):
        # Scope is WRITE-ONCE here, the posture the vector store already documents:
        # re-scoping is a delete plus re-add, not an enrichment. Submitting a scope
        # for a rule stored globally yields a second, scoped lesson.
        store = self._store(tmp_path)
        store.save_or_enrich(Lesson(ts="t", rule="r", category="tool"))
        assert (
            store.save_or_enrich(Lesson(ts="t", rule="r", category="tool", repo_scope="src/pkg"))
            == "inserted"
        )
        scopes = sorted((le.repo_scope or "") for le in store.load_all())
        assert scopes == ["", "src/pkg"]

    def test_a_stored_scope_is_never_stripped(self, tmp_path):
        # The guarantee that matters: no later write quietly removes a scope already
        # on a row.
        store = self._store(tmp_path)
        store.save_or_enrich(Lesson(ts="t", rule="r", category="tool", repo_scope="src/pkg"))
        store.save_or_enrich(Lesson(ts="t", rule="r", category="tool"))
        assert len([le for le in store.load_all() if le.repo_scope == "src/pkg"]) == 1

    def test_a_malformed_scope_row_is_dropped(self, tmp_path):
        # Same rule on the JSONL side: a present non-string scope is neither global
        # nor handed to the gate (where it would raise mid-prompt-assembly).
        (tmp_path / "lessons.jsonl").write_text(
            json.dumps({"ts": "t", "rule": "listy scope", "category": "tool", "repo_scope": []})
            + "\n"
            + json.dumps(
                {"ts": "t", "rule": "stringy scope", "category": "tool", "repo_scope": ["a"]}
            )
            + "\n"
            + json.dumps({"ts": "t", "rule": "fine", "category": "tool"})
            + "\n",
            encoding="utf-8",
        )
        store = LessonStore(base_dir=tmp_path)
        rules = [le.rule for le in store.load_all()]
        assert rules == ["fine"]
        assert "listy scope" not in store.get_context()

    def test_a_second_repo_gets_its_own_row_not_a_stolen_one(self, tmp_path):
        # Scope is part of identity. Saving the same rule for repo B must not take
        # repo A's row: A would lose the lesson entirely.
        store = self._store(tmp_path)
        store.save_or_enrich(Lesson(ts="t", rule="bump first", category="tool", repo_scope="a"))
        assert (
            store.save_or_enrich(Lesson(ts="t", rule="bump first", category="tool", repo_scope="b"))
            == "inserted"
        )
        scopes = sorted(le.repo_scope for le in store.load_all())
        assert scopes == ["a", "b"]

    def test_an_unscoped_save_does_not_claim_a_scoped_row(self, tmp_path):
        # Strict identity, matching the vector store: (rule, None) and (rule, "a")
        # are two lessons. My earlier guard treated an omitted scope as "no
        # conflict", so a genuine save-this-globally request bound to the scoped row
        # and reported success without ever creating the global lesson.
        store = self._store(tmp_path)
        store.save_or_enrich(Lesson(ts="t", rule="r", category="tool", repo_scope="a"))
        assert store.save_or_enrich(Lesson(ts="t", rule="r", category="tool")) == "inserted"
        scopes = sorted((le.repo_scope or "") for le in store.load_all())
        assert scopes == ["", "a"]

    def test_addressing_a_scoped_lesson_means_naming_its_scope(self, tmp_path):
        # The other side of strict identity: a clause attaches when the submission
        # names the same scope, and only then.
        store = self._store(tmp_path)
        store.save_or_enrich(Lesson(ts="t", rule="r", category="tool", repo_scope="a"))
        assert (
            store.save_or_enrich(
                Lesson(ts="t", rule="r", category="tool", negative="not this", repo_scope="a")
            )
            == "enriched"
        )
        rows = store.load_all()
        assert len(rows) == 1
        assert (rows[0].repo_scope, rows[0].negative) == ("a", "not this")

    def test_a_scoped_submission_carries_its_own_clause(self, tmp_path):
        # A scoped submission is its own lesson, so its NOT-clause lands on that new
        # row and the pre-existing global lesson is untouched.
        store = self._store(tmp_path)
        store.save_or_enrich(Lesson(ts="t", rule="r", category="tool"))
        store.save_or_enrich(
            Lesson(ts="t", rule="r", category="tool", negative="not this", repo_scope="src/pkg")
        )
        rows = {le.repo_scope: le.negative for le in store.load_all()}
        assert rows == {None: None, "src/pkg": "not this"}


class TestVectorStoreLessonScope:
    """The vector store is the PRIMARY injection path, so the gate must hold here.

    Filtering only the JSONL fallback would leave the scope key inert in
    production -- advertised and enforced nowhere, which is the failure this
    whole mechanism exists to end.
    """

    def _store(self, tmp_path):
        from kiro_crew.vector_memory import VectorMemoryStore

        store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
        store.init()
        return store

    def test_scoped_lesson_reaches_only_its_repo(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / "src" / "pkg").mkdir(parents=True)
        outside = tmp_path / "other"
        outside.mkdir()
        store = self._store(tmp_path)
        try:
            assert store.write_lesson("gate the repo", "tool", None, repo_scope="src/pkg")
            assert "gate the repo" in store.get_lessons_context(project_dir=repo)
            assert "gate the repo" not in store.get_lessons_context(project_dir=outside)
            # Fail-closed with no project at all.
            assert store.get_lessons_context() == ""
        finally:
            store.close()

    def test_unscoped_lesson_is_unaffected(self, tmp_path):
        store = self._store(tmp_path)
        try:
            assert store.write_lesson("applies anywhere", "tool")
            assert "applies anywhere" in store.get_lessons_context()
        finally:
            store.close()

    def test_a_scoped_write_does_not_supersede_a_global_lesson(self, tmp_path):
        # Deliberately the near-identical wording that the generic dedup rules
        # (substring / keyword overlap / cosine) treat as the same lesson. Across
        # scopes they are NOT, and superseding here would delete global guidance the
        # scoped write never addressed.
        store = self._store(tmp_path)
        try:
            assert store.write_lesson("visible rule", "tool")
            assert store.write_lesson("hidden rule", "tool", None, repo_scope="src/pkg")
            texts = [_lesson_display_text(json.loads(r["value_json"])) for r in store.get_lessons()]
            assert "visible rule" in texts
            assert "hidden rule" in texts
        finally:
            store.close()

    def test_the_same_rule_in_two_scopes_is_two_rows(self, tmp_path):
        # Same rule text, different scope. A shared key would let the second write
        # overwrite the first and silently re-scope it.
        store = self._store(tmp_path)
        try:
            assert store.write_lesson("run the gate", "tool")
            assert store.write_lesson("run the gate", "tool", None, repo_scope="src/pkg")
            rows = store.get_lessons()
            scopes = {_lesson_scope(json.loads(r["value_json"])) for r in rows}
            assert scopes == {None, "src/pkg"}
            assert len({r["key"] for r in rows}) == 2
        finally:
            store.close()

    def test_an_unscoped_lesson_keeps_its_historical_key(self, tmp_path):
        # The unscoped key must not move, or every stored row would need migrating.
        store = self._store(tmp_path)
        try:
            store.write_lesson("keep my key", "tool")
            assert store.get_lessons()[0]["key"] == f"lesson.{_lesson_slug('keep my key')}"
        finally:
            store.close()

    def test_the_contradiction_sweep_is_scope_local(self, tmp_path):
        # Superseding resolves a contradiction by DELETING the loser, so a scoped
        # exception must not put a global rule up for deletion: it contradicts that
        # rule only inside its own tree. A global rule retired on the strength of
        # one repository's exception is gone for every other repository.
        store = self._store(tmp_path)
        try:
            # Without an embedder the scan returns early, so the loop under test
            # would never run. A constant vector makes every pair score 1.0, which
            # keeps the assertion about SCOPE rather than about similarity.
            store.embed_fn = lambda text: [1.0, 0.0, 0.0, 0.0]
            store.write_lesson("never commit directly to main", "tool")
            emb = [1.0, 0.0, 0.0, 0.0]
            scoped = store.find_contradiction_candidates(
                "commit hotfixes directly to main", 0.0, 1.5, emb, "src/pkg"
            )
            unscoped = store.find_contradiction_candidates(
                "commit hotfixes directly to main", 0.0, 1.5, emb, None
            )
            assert [c["rule"] for c in unscoped] == ["never commit directly to main"]
            assert scoped == []
        finally:
            store.close()

    def test_migration_skips_a_malformed_scope_instead_of_globalising_it(
        self, tmp_path, monkeypatch
    ):
        # migrate_from_markdown reads lessons.jsonl directly, so it needs the same
        # three-state rule as the store: a present unusable scope is skipped, not
        # normalised to None and injected everywhere.
        from kiro_crew import vector_memory as vm

        home = tmp_path / "home"
        home.mkdir()
        (home / "lessons.jsonl").write_text(
            json.dumps({"ts": "t", "rule": "listy", "category": "tool", "repo_scope": []})
            + "\n"
            + json.dumps({"ts": "t", "rule": "fine", "category": "tool"})
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(vm, "config_dir", lambda: home)
        store = self._store(tmp_path)
        try:
            counts = store.migrate_from_markdown()
            rules = [_lesson_display_text(json.loads(r["value_json"])) for r in store.get_lessons()]
            assert rules == ["fine"]
            assert counts["skipped"] >= 1
        finally:
            store.close()

    def test_migration_preserves_a_scoped_lesson(self, tmp_path, monkeypatch):
        # A scoped lesson that migrates in as global is silently widened, which is
        # the one direction the gate must never move. Drives the real
        # migrate_from_markdown, which reads its source dir from config_dir().
        from kiro_crew import vector_memory as vm

        home = tmp_path / "home"
        home.mkdir()
        (home / "lessons.jsonl").write_text(
            json.dumps(
                {
                    "ts": "t",
                    "rule": "bump the manifest before tagging",
                    "category": "tool",
                    "negative": None,
                    "repo_scope": "src/pkg",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(vm, "config_dir", lambda: home)
        store = self._store(tmp_path)
        try:
            counts = store.migrate_from_markdown()
            assert counts["semantic"] >= 1
            scopes = {_lesson_scope(json.loads(r["value_json"])) for r in store.get_lessons()}
            assert scopes == {"src/pkg"}
        finally:
            store.close()

    def _plant_malformed(self, store, rule):
        """Store *rule* then corrupt its scope to a present-but-unusable value."""
        store.write_lesson(rule, "tool")
        row = store.get_lessons()[0]
        value = json.loads(row["value_json"])
        value["repo_scope"] = []
        store.db.execute(
            "UPDATE semantic_memory SET value_json = ? WHERE key = ?",
            (json.dumps(value), row["key"]),
        )
        store.db.commit()

    def test_a_malformed_row_does_not_dedup_away_a_global_write(self, tmp_path):
        # The row is withheld at injection, so if the dedup scan read it as unscoped
        # a genuine global write would be discarded against it and the caller told
        # the lesson was saved while nothing reaches a prompt.
        store = self._store(tmp_path)
        try:
            self._plant_malformed(store, "always rebase before pushing")
            assert store.write_lesson("always rebase before pushing", "tool") is True
            assert "always rebase before pushing" in store.get_lessons_context()
        finally:
            store.close()

    def test_a_malformed_row_does_not_count_as_population(self, tmp_path):
        # It renders fine but is never injected, so counting it would silence the
        # JSONL store while nothing reaches the prompt.
        store = self._store(tmp_path)
        try:
            self._plant_malformed(store, "some rule")
            assert store.has_any_lesson() is False
        finally:
            store.close()

    def test_a_trailing_slash_is_the_same_scope_in_both_stores(self, tmp_path):
        # The gate strips separators before matching, so these two spellings behave
        # identically. Storing them raw made two rows that were both injected and
        # neither deduped the other.
        store = self._store(tmp_path)
        try:
            assert store.write_lesson("r", "tool", repo_scope="src/pkg") is True
            store.write_lesson("r", "tool", repo_scope="src/pkg/")
            assert len(store.get_lessons()) == 1
        finally:
            store.close()

    def test_a_near_limit_multibyte_scoped_lesson_is_still_accepted(self, tmp_path):
        # Adding repo_scope to the mapping must not drop the lesson out of the size
        # exemption: the envelope would then be measured and a rule that the bare
        # form always allowed would be refused, while a caller with a JSONL fallback
        # reported it saved.
        store = self._store(tmp_path)
        try:
            rule = "\u4e00" * 500
            negative = "\u4e00" * 500
            assert store.write_lesson(rule, "tool", negative=negative, repo_scope="src/pkg") is True
            assert len(store.get_lessons()) == 1
        finally:
            store.close()

    def test_an_inadmissible_scope_is_refused_not_laundered(self, tmp_path):
        # Two of this change's own rules disagreed on "/src/pkg": the canonicaliser
        # strips the leading slash (making it admissible) while the gate refuses it.
        # The write surface must not resolve that by normalising -- that ACTIVATES
        # the lesson in every repository holding src/pkg -- nor by dropping the
        # scope, which stores it globally. It refuses.
        store = self._store(tmp_path)
        try:
            assert store.write_lesson("r", "tool", repo_scope="/src/pkg") is False
            assert store.get_lessons() == []
            # A trailing slash is an equivalent SPELLING, not an invalid scope, so it
            # still folds and still writes.
            assert store.write_lesson("r", "tool", repo_scope="src/pkg/") is True
            assert _lesson_scope(json.loads(store.get_lessons()[0]["value_json"])) == "src/pkg"
        finally:
            store.close()

    def test_no_caller_can_launder_an_inadmissible_scope(self, tmp_path, monkeypatch):
        # The write surface is the single chokepoint, so the migration is covered by
        # it rather than by its own guard -- verified by probe: reverting the
        # migration's condition alone does NOT reopen this. That is the point. The
        # migration's condition is kept so its own skip COUNT is right for the right
        # reason, but the property below holds for every caller, including ones this
        # PR never touched.
        from kiro_crew import vector_memory as vm

        home = tmp_path / "home"
        home.mkdir()
        (home / "lessons.jsonl").write_text(
            json.dumps({"ts": "t", "rule": "r", "category": "tool", "repo_scope": "/src/pkg"})
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(vm, "config_dir", lambda: home)
        store = self._store(tmp_path / "vs")
        try:
            store.migrate_from_markdown()
            # Neither activated in src/pkg nor widened to global: not stored at all.
            assert store.get_lessons() == []
        finally:
            store.close()

    def test_a_stale_client_legacy_scope_is_refused_not_globalised(self):
        # The fail direction matters more than the refusal: before this change the
        # workspace tier was inert, so converting it to a global lesson would inject
        # a one-workspace correction into every session -- worse than the dead tier
        # it replaced.
        from kiro_crew.mcp_tools import learn as mcp_learn

        out = mcp_learn.learn_add("learn_add", {"rule": "r", "scope": "workspace"})
        assert out.startswith("Error:")
        assert "repo_scope" in out
        # An explicit global, and an absent scope, are both still fine.
        for args in ({"rule": "r", "scope": "global"}, {"rule": "r"}):
            assert not mcp_learn.learn_add("learn_add", args).startswith("Error: scope=")

    def test_a_gate_invalid_string_scope_does_not_count_as_population(self, tmp_path):
        # "." is a non-blank string, so judging by shape called it usable while the
        # gate refuses it. The row then rendered nothing yet still counted as stored
        # knowledge, which silenced the JSONL store and lost the saved lessons.
        # "/etc" is the same hole reached differently: an imported row skips the
        # write surface, so it keeps a leading slash the gate refuses.
        for bad in (".", "..", "/etc", "  "):
            store = self._store(tmp_path / f"s{abs(hash(bad))}")
            try:
                store.write_lesson("some rule", "tool")
                row = store.get_lessons()[0]
                value = json.loads(row["value_json"])
                value["repo_scope"] = bad
                store.db.execute(
                    "UPDATE semantic_memory SET value_json = ? WHERE key = ?",
                    (json.dumps(value), row["key"]),
                )
                store.db.commit()
                assert store.has_any_lesson() is False, bad
            finally:
                store.close()

    def test_a_junk_lesson_row_does_not_count_as_population(self, tmp_path):
        # A lesson.* key holding non-lesson data is skipped by every renderer, so
        # counting it as population would silence the JSONL store while nothing
        # renders and saved corrections would vanish.
        store = self._store(tmp_path)
        try:
            assert store.has_any_lesson() is False
            store.set_semantic("lesson.junk", ["not", "a", "lesson"], 1.0, "import")
            assert store.has_any_lesson() is False
            assert store.write_lesson("a real one", "tool")
            assert store.has_any_lesson() is True
        finally:
            store.close()

    def test_a_malformed_scope_is_withheld_not_globalised(self, tmp_path):
        # A PRESENT but unusable scope (a list from an imported or hand-edited row)
        # must not read as "applies everywhere". The row meant to be scoped and
        # cannot say where, so withholding is the only safe answer.
        store = self._store(tmp_path)
        try:
            store.write_lesson("keep me out", "tool")
            row = store.get_lessons()[0]
            value = json.loads(row["value_json"])
            value["repo_scope"] = []
            store.db.execute(
                "UPDATE semantic_memory SET value_json = ? WHERE key = ?",
                (json.dumps(value), row["key"]),
            )
            store.db.commit()
            assert "keep me out" not in store.get_lessons_context(project_dir=tmp_path)
            assert "keep me out" not in store.get_lessons_context()
        finally:
            store.close()

    def test_out_of_scope_lesson_is_not_counted_as_omitted(self, tmp_path):
        # "omitted" means "did not fit the budget". Reporting an out-of-scope
        # lesson there would tell the model rules are being withheld for space.
        repo = tmp_path / "repo"
        (repo / "src" / "pkg").mkdir(parents=True)
        outside = tmp_path / "other"
        outside.mkdir()
        store = self._store(tmp_path)
        try:
            # Deliberately unrelated wording: write_lesson's dedup replaces an
            # older lesson that shares most of its significant words, which would
            # collapse two similarly-worded rules into one row.
            store.write_lesson("prefer tabs over spaces", "preference")
            store.write_lesson("bump the manifest version", "tool", None, repo_scope="src/pkg")
            out = store.get_lessons_context(project_dir=outside)
            assert "prefer tabs over spaces" in out
            assert "bump the manifest version" not in out
            assert "omitted" not in out
        finally:
            store.close()
