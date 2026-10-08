"""Cited code on lessons: recorded at write, rechecked when a prompt is built.

A lesson that describes a repository's code records each cited file's content hash
and the commit the repository was at. When a prompt is assembled, a row whose cited
file is gone is withheld and reported on its own, one whose cited file changed is
injected with a note, and one that still matches (or cites nothing) renders exactly
as it always did.
"""

from __future__ import annotations

import json
import subprocess
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from conftest import requires_symlinks
from kiro_crew.history import ConversationLog
from kiro_crew.learn import Lesson, LessonStore
from kiro_crew.lesson_cites import (
    CITE_MAX,
    CiteReview,
    capture_cites,
    commit_in_repo,
    normalize_cited_commit,
    normalize_cites,
    read_head_commit,
)
from kiro_crew.project_scope import find_repo_root, project_scope_satisfied, resolve_in_project
from kiro_crew.vector_memory import LessonWriteOutcome, VectorMemoryStore


@pytest.fixture(autouse=True)
def _a_session_is_rendering():
    """A recheck asks governance for the session being rendered; with none named it
    denies, so the tests that exercise rechecks name one (permitted by default)."""
    from kiro_crew.lesson_cites import cite_session

    with cite_session("test-session"):
        yield


COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
RULE = "never call save from inside the lock"
SOURCE = "src/pkg/mod.py"


def _repo(
    tmp_path: Path, name: str = "repo", body: str = "print(1)\n", *, with_commit: bool = True
) -> Path:
    """A repository holding one source file, with a detached HEAD.

    With ``with_commit`` the HEAD commit is also an object of the repository, as in a
    real checkout; a fixture standing for ANOTHER checkout leaves it out.
    """
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text(COMMIT + "\n")
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / SOURCE).write_text(body)
    if with_commit:
        _loose_object(repo, COMMIT)
    return repo


def _cite(repo: Path, path: str = SOURCE) -> list[dict[str, str]]:
    captured = capture_cites([path], [], repo)
    assert captured.cites and not captured.refused
    return captured.cites


def _vector_store(tmp_path: Path) -> VectorMemoryStore:
    store = VectorMemoryStore(db_path=tmp_path / "m.db", embedding_dim=4)
    store.init()
    return store


class TestResolveInProject:
    def test_the_satisfied_gate_is_a_view_of_the_resolver(self, tmp_path):
        repo = _repo(tmp_path)
        assert resolve_in_project(SOURCE, repo) == (repo / SOURCE).resolve()
        assert project_scope_satisfied(SOURCE, repo) is True
        assert resolve_in_project("src/missing.py", repo) is None
        assert project_scope_satisfied("src/missing.py", repo) is False

    def test_a_refusal_never_says_why(self, tmp_path):
        repo = _repo(tmp_path)
        assert resolve_in_project("../outside", repo) is None
        assert resolve_in_project("/etc/passwd", repo) is None
        assert resolve_in_project(SOURCE, None) is None
        assert resolve_in_project(SOURCE, "relative/dir") is None

    def test_the_repository_root_is_found_from_a_subdirectory(self, tmp_path):
        repo = _repo(tmp_path)
        assert find_repo_root(repo / "src" / "pkg") == repo.resolve()

    def test_no_repository_means_no_root(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        # A real ``.git`` can sit above a temp root; only the absence of one here
        # is asserted, through the project's own walk bound.
        assert find_repo_root(None) is None
        assert find_repo_root("relative") is None
        assert find_repo_root(plain) in (None, *plain.resolve().parents)


class TestNormalizers:
    def test_a_well_formed_cite_list_round_trips(self):
        raw = [{"path": SOURCE, "sha256": "c" * 64}]
        assert normalize_cites(raw) == raw

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "src/a.py",
            [],
            [{"path": SOURCE}],
            [{"path": SOURCE, "sha256": "short"}],
            [{"path": "../a.py", "sha256": "c" * 64}],
            [{"path": "/abs/a.py", "sha256": "c" * 64}],
            [{"path": "a]b.py", "sha256": "c" * 64}],
            [{"path": "a\nb.py", "sha256": "c" * 64}],
            [{"path": "a b.py", "sha256": "c" * 64}],
            ["src/a.py"],
        ],
    )
    def test_an_unusable_list_reads_as_no_cites(self, raw):
        assert normalize_cites(raw) is None

    def test_duplicates_and_overflow_are_dropped(self):
        entry = {"path": SOURCE, "sha256": "c" * 64}
        assert normalize_cites([entry, dict(entry)]) == [entry]
        many = [{"path": f"src/f{i}.py", "sha256": "c" * 64} for i in range(CITE_MAX + 3)]
        assert len(normalize_cites(many) or []) == CITE_MAX

    def test_only_a_git_object_name_is_a_commit(self):
        assert normalize_cited_commit(COMMIT) == COMMIT
        assert normalize_cited_commit("c" * 64) == "c" * 64
        assert normalize_cited_commit(COMMIT + "\n") is None
        assert normalize_cited_commit("HEAD") is None
        assert normalize_cited_commit(None) is None


class TestReadHeadCommit:
    def test_a_detached_head(self, tmp_path):
        assert read_head_commit(_repo(tmp_path)) == COMMIT

    def test_a_loose_ref(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (repo / ".git" / "refs" / "heads").mkdir(parents=True)
        (repo / ".git" / "refs" / "heads" / "main").write_text(OTHER_COMMIT + "\n")
        assert read_head_commit(repo) == OTHER_COMMIT

    def test_a_packed_ref(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (repo / ".git" / "packed-refs").write_text(
            f"# pack-refs\n{OTHER_COMMIT} refs/heads/main\n{COMMIT} refs/heads/other\n"
        )
        assert read_head_commit(repo) == OTHER_COMMIT

    def test_a_worktree_pointer_file_with_a_common_directory(self, tmp_path):
        main = _repo(tmp_path, "main")
        (main / ".git" / "refs" / "heads").mkdir(parents=True)
        (main / ".git" / "refs" / "heads" / "feat").write_text(OTHER_COMMIT + "\n")
        admin = main / ".git" / "worktrees" / "wt"
        admin.mkdir(parents=True)
        (admin / "HEAD").write_text("ref: refs/heads/feat\n")
        (admin / "commondir").write_text("../..\n")
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {admin}\n")
        assert read_head_commit(wt) == OTHER_COMMIT

    def test_a_ref_that_escapes_the_git_directory_is_refused(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / ".git" / "HEAD").write_text("ref: refs/../../../etc/passwd\n")
        assert read_head_commit(repo) is None

    def test_a_missing_or_unreadable_head_is_none(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / ".git" / "HEAD").write_text("not a commit\n")
        assert read_head_commit(repo) is None
        assert read_head_commit(None) is None
        assert read_head_commit(tmp_path / "nowhere") is None


class TestCaptureCites:
    def test_an_explicit_cite_records_its_hash_and_the_commit(self, tmp_path):
        repo = _repo(tmp_path)
        captured = capture_cites([SOURCE], [], repo)
        assert captured.cites is not None and captured.cites[0]["path"] == SOURCE
        assert len(captured.cites[0]["sha256"]) == 64
        assert captured.cited_commit == COMMIT
        assert captured.refused == ()

    def test_the_hash_follows_the_content(self, tmp_path):
        repo = _repo(tmp_path)
        before = _cite(repo)
        (repo / SOURCE).write_text("print(2)\n")
        assert _cite(repo) != before

    def test_nothing_cited_records_nothing(self, tmp_path):
        repo = _repo(tmp_path)
        captured = capture_cites([], ["a rule that names no file"], repo)
        assert captured.cites is None
        assert captured.cited_commit is None

    def test_a_path_named_in_the_rule_text_is_recorded_when_it_resolves(self, tmp_path):
        repo = _repo(tmp_path)
        captured = capture_cites([], [f"stop editing {SOURCE}:12 by hand"], repo)
        assert [c["path"] for c in captured.cites or []] == [SOURCE]

    def test_a_path_in_the_text_that_does_not_resolve_is_prose_not_a_refusal(self, tmp_path):
        repo = _repo(tmp_path)
        captured = capture_cites([], ["see docs/guide/missing.md and src/nowhere.py"], repo)
        assert captured.cites is None
        assert captured.refused == ()

    def test_an_explicit_and_an_in_text_mention_of_one_file_is_one_cite(self, tmp_path):
        repo = _repo(tmp_path)
        captured = capture_cites([SOURCE], [f"the {SOURCE} module"], repo)
        assert len(captured.cites or []) == 1

    @pytest.mark.parametrize(
        "bad",
        ["src/missing.py", "../outside.py", "/etc/passwd", "src/pkg", "a]b.py", "", "   "],
    )
    def test_a_cite_that_is_not_a_file_in_the_project_is_refused_by_name(self, tmp_path, bad):
        repo = _repo(tmp_path)
        captured = capture_cites([bad, SOURCE], [], repo)
        assert [c["path"] for c in captured.cites or []] == [SOURCE]
        assert [path for path, _ in captured.refused] == [bad[:256]]

    def test_a_missing_and_a_sensitive_path_are_refused_with_the_same_reason(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import project_scope as ps

        # The predicate is home-relative, so a fixture under a temp root is flagged
        # by segment, as the project-scope tests do for the real home.
        monkeypatch.setattr(
            ps, "is_sensitive_path", lambda p, base_dir=None: ".ssh" in Path(p).parts
        )
        repo = _repo(tmp_path)
        (repo / ".ssh").mkdir()
        (repo / ".ssh" / "id_rsa").write_text("secret")
        reasons = {
            path: reason
            for path, reason in capture_cites([".ssh/id_rsa", "src/gone.py"], [], repo).refused
        }
        assert reasons[".ssh/id_rsa"] == reasons["src/gone.py"]

    @requires_symlinks
    def test_a_symlink_out_of_the_project_is_refused(self, tmp_path):
        repo = _repo(tmp_path)
        outside = tmp_path / "outside.py"
        outside.write_text("x = 1\n")
        (repo / "src" / "link.py").symlink_to(outside)
        captured = capture_cites(["src/link.py"], [], repo)
        assert captured.cites is None
        assert [path for path, _ in captured.refused] == ["src/link.py"]

    def test_no_project_refuses_an_explicit_cite_and_says_so(self, tmp_path):
        captured = capture_cites([SOURCE], [], None)
        assert captured.cites is None
        assert "no project" in captured.refused[0][1]

    def test_a_relative_project_is_not_resolved_against_the_cwd(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        monkeypatch.chdir(repo)
        assert capture_cites([SOURCE], [], ".").cites is None

    def test_a_file_over_the_bound_cannot_be_cited(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "src" / "big.py").write_bytes(b"x" * ((1 << 20) + 1))
        captured = capture_cites(["src/big.py"], [], repo)
        assert captured.cites is None
        assert "larger" in captured.refused[0][1]

    def test_a_lesson_may_cite_at_most_the_limit(self, tmp_path):
        repo = _repo(tmp_path)
        names = []
        for i in range(CITE_MAX + 2):
            (repo / "src" / f"f{i}.py").write_text(str(i))
            names.append(f"src/f{i}.py")
        captured = capture_cites(names, [], repo)
        assert len(captured.cites or []) == CITE_MAX
        assert [path for path, _ in captured.refused] == ["(further cites)"]

    def test_a_cite_names_the_same_file_from_the_root_and_from_a_subdirectory(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "src" / "pkg" / "src").mkdir()
        (repo / "src" / "pkg" / "src" / "pkg").mkdir()
        (repo / "src" / "pkg" / SOURCE).parent.mkdir(parents=True, exist_ok=True)
        (repo / "src" / "pkg" / SOURCE).write_text("a different file\n")
        from_root = capture_cites([SOURCE], [], repo).cites
        from_sub = capture_cites([SOURCE], [], repo / "src" / "pkg").cites
        assert from_root == from_sub


class TestCiteReview:
    def test_a_file_that_still_matches_is_kept_without_a_note(self, tmp_path):
        repo = _repo(tmp_path)
        review = CiteReview(repo)
        assert review.annotate(_cite(repo), scope_satisfied=False) == (True, "")
        assert review.withheld == 0

    def test_a_changed_file_is_kept_with_a_note_naming_it(self, tmp_path):
        repo = _repo(tmp_path)
        cites = _cite(repo)
        (repo / SOURCE).write_text("print(2)\n")
        keep, note = CiteReview(repo).annotate(cites, scope_satisfied=False)
        assert keep is True
        assert note == f" [cited code changed since learned: {SOURCE}]"

    def test_a_gone_file_withholds_the_row_and_counts_it(self, tmp_path):
        repo = _repo(tmp_path)
        cites = _cite(repo)
        (repo / SOURCE).unlink()
        review = CiteReview(repo)
        assert review.annotate(cites, scope_satisfied=True) == (False, "")
        assert review.withheld == 1

    def test_a_row_with_no_cites_is_never_checked(self, tmp_path):
        review = CiteReview(_repo(tmp_path))
        assert review.annotate(None, scope_satisfied=True) == (True, "")
        assert review.annotate([], scope_satisfied=True) == (True, "")

    def test_a_session_with_no_project_checks_nothing(self, tmp_path):
        repo = _repo(tmp_path)
        cites = _cite(repo)
        (repo / SOURCE).unlink()
        assert CiteReview(None).annotate(cites, scope_satisfied=False) == (True, "")

    def test_another_checkout_where_nothing_resolves_is_not_read_as_missing(self, tmp_path):
        repo = _repo(tmp_path)
        cites = _cite(repo)
        elsewhere = _repo(tmp_path, "elsewhere", with_commit=False)
        (elsewhere / SOURCE).unlink()
        review = CiteReview(elsewhere)
        assert review.annotate(cites, scope_satisfied=False) == (True, "")
        assert review.withheld == 0

    def test_a_satisfied_scope_establishes_the_repository_even_if_every_cite_is_gone(
        self, tmp_path
    ):
        repo = _repo(tmp_path)
        cites = _cite(repo)
        (repo / SOURCE).unlink()
        review = CiteReview(repo)
        assert review.annotate(cites, scope_satisfied=True)[0] is False

    def test_a_file_two_rows_cite_is_hashed_once(self, tmp_path):
        repo = _repo(tmp_path)
        cites = _cite(repo)
        review = CiteReview(repo)
        with patch("kiro_crew.lesson_cites._file_digest", wraps=lambda p: "0" * 64) as digest:
            review.annotate(cites, scope_satisfied=True)
            review.annotate(cites, scope_satisfied=True)
        assert digest.call_count == 1

    def test_a_note_names_at_most_two_files(self, tmp_path):
        repo = _repo(tmp_path)
        names = []
        for i in range(3):
            (repo / "src" / f"f{i}.py").write_text("a")
            names.append(f"src/f{i}.py")
        cites = capture_cites(names, [], repo).cites
        for name in names:
            (repo / name).write_text("b")
        _, note = CiteReview(repo).annotate(cites, scope_satisfied=False)
        assert note == " [cited code changed since learned: src/f0.py, src/f1.py, …]"


class TestJsonlStore:
    """The JSONL store: stored shape, enrichment, and the injected block."""

    def _store(self, tmp_path: Path) -> LessonStore:
        return LessonStore(base_dir=tmp_path / "home")

    def _lesson(self, repo: Path, **fields) -> Lesson:
        cites = _cite(repo)
        return Lesson(
            ts="t",
            rule=RULE,
            category="tool",
            cites=cites,
            cited_commit=COMMIT,
            **fields,
        )

    def test_a_row_without_cites_is_stored_byte_identically(self, tmp_path):
        store = self._store(tmp_path)
        store.save(Lesson(ts="t", rule=RULE, category="tool"))
        row = json.loads(store.path.read_text().splitlines()[0])
        assert set(row) == {"ts", "rule", "category", "negative", "repo_scope"}

    def test_cites_round_trip(self, tmp_path):
        repo = _repo(tmp_path)
        store = self._store(tmp_path)
        store.save(self._lesson(repo))
        (loaded,) = store.load_all()
        assert loaded.cites == _cite(repo)
        assert loaded.cited_commit == COMMIT

    def test_a_hand_edited_row_with_unusable_cites_still_loads_as_uncited(self, tmp_path):
        store = self._store(tmp_path)
        store.save(Lesson(ts="t", rule=RULE, category="tool"))
        row = json.loads(store.path.read_text())
        row["cites"] = "not a list"
        row["cited_commit"] = COMMIT
        store.path.write_text(json.dumps(row) + "\n")
        fresh = self._store(tmp_path)
        (loaded,) = fresh.load_all()
        assert loaded.cites is None and loaded.cited_commit is None

    def test_a_current_row_renders_exactly_as_an_uncited_one_does(self, tmp_path):
        repo = _repo(tmp_path)
        cited = self._store(tmp_path / "a")
        cited.save(self._lesson(repo))
        plain = self._store(tmp_path / "b")
        plain.save(Lesson(ts="t", rule=RULE, category="tool"))
        assert cited.get_context(project_dir=repo) == plain.get_context(project_dir=repo)

    def test_a_changed_file_adds_the_note_to_the_rule(self, tmp_path):
        repo = _repo(tmp_path)
        store = self._store(tmp_path)
        store.save(self._lesson(repo))
        (repo / SOURCE).write_text("print(2)\n")
        out = store.get_context(project_dir=repo)
        assert f"- {RULE} [cited code changed since learned: {SOURCE}]\n" in out

    def test_the_note_does_not_score_against_the_request(self, tmp_path):
        repo = _repo(tmp_path)
        store = self._store(tmp_path)
        store.save(self._lesson(repo, applies="on_topic"))
        (repo / SOURCE).write_text("print(2)\n")
        # "changed code learned" are the note's words, not the lesson's.
        out = store.get_context(project_dir=repo, query_text="changed code learned cited")
        assert "Withheld all" in out and RULE not in out

    def test_a_gone_file_withholds_the_row_and_reports_it_separately(self, tmp_path):
        repo = _repo(tmp_path)
        store = self._store(tmp_path)
        store.save(self._lesson(repo, repo_scope="src/pkg"))
        store.save(Lesson(ts="t", rule="prefer tabs over spaces", category="preference"))
        (repo / SOURCE).unlink()
        out = store.get_context(project_dir=repo)
        assert RULE not in out
        assert "prefer tabs over spaces" in out
        assert "[Withheld 1 learned rule: the code it cites no longer exists" in out
        assert "omitted" not in out

    def test_every_row_withheld_still_reports_the_count(self, tmp_path):
        repo = _repo(tmp_path)
        store = self._store(tmp_path)
        store.save(self._lesson(repo, repo_scope="src/pkg"))
        (repo / SOURCE).unlink()
        assert store.get_context(project_dir=repo).startswith("[Withheld 1 learned rule")

    def test_an_unscoped_row_whose_only_cite_is_gone_renders_as_it_always_did(self, tmp_path):
        # With no scope, no cite that still resolves and a commit this repository does
        # not hold, nothing says the session is in the cited repository, so the row
        # cannot be told from one learned in another checkout.
        repo = _repo(tmp_path, with_commit=False)
        store = self._store(tmp_path)
        store.save(self._lesson(repo))
        (repo / SOURCE).unlink()
        out = store.get_context(project_dir=repo)
        assert f"- {RULE}\n" in out and "Withheld" not in out

    def test_a_second_cite_that_still_resolves_establishes_the_repository(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "src" / "other.py").write_text("x = 1\n")
        store = self._store(tmp_path)
        cites = capture_cites([SOURCE, "src/other.py"], [], repo).cites
        store.save(Lesson(ts="t", rule=RULE, category="tool", cites=cites))
        (repo / SOURCE).unlink()
        assert "[Withheld 1 learned rule" in store.get_context(project_dir=repo)

    def test_another_checkout_renders_the_row_as_it_always_did(self, tmp_path):
        repo = _repo(tmp_path)
        store = self._store(tmp_path)
        store.save(self._lesson(repo))
        elsewhere = _repo(tmp_path, "elsewhere", with_commit=False)
        (elsewhere / SOURCE).unlink()
        out = store.get_context(project_dir=elsewhere)
        assert f"- {RULE}\n" in out and "Withheld" not in out

    def test_no_project_renders_the_row_as_it_always_did(self, tmp_path):
        repo = _repo(tmp_path)
        store = self._store(tmp_path)
        store.save(self._lesson(repo))
        (repo / SOURCE).unlink()
        assert f"- {RULE}\n" in store.get_context()

    def test_a_resubmit_with_new_cites_enriches_the_row(self, tmp_path):
        repo = _repo(tmp_path)
        store = self._store(tmp_path)
        store.save(Lesson(ts="t", rule=RULE, category="tool"))
        assert store.save_or_enrich(self._lesson(repo)) == "enriched"
        (loaded,) = store.load_all()
        assert loaded.cites == _cite(repo) and loaded.cited_commit == COMMIT

    def test_a_resubmit_of_the_same_cites_changes_nothing(self, tmp_path):
        repo = _repo(tmp_path)
        store = self._store(tmp_path)
        store.save(self._lesson(repo))
        before = store.path.read_text()
        again = self._lesson(repo)
        again.cited_commit = OTHER_COMMIT
        assert store.save_or_enrich(again) == "unchanged"
        assert store.path.read_text() == before

    def test_a_resubmit_without_cites_never_strips_them(self, tmp_path):
        repo = _repo(tmp_path)
        store = self._store(tmp_path)
        store.save(self._lesson(repo))
        outcome = store.save_or_enrich(Lesson(ts="t", rule=RULE, category="tool", negative="x"))
        assert outcome == "enriched"
        (loaded,) = store.load_all()
        assert loaded.cites == _cite(repo) and loaded.negative == "x"

    def test_a_resubmit_after_the_file_changed_refreshes_the_hash(self, tmp_path):
        repo = _repo(tmp_path)
        store = self._store(tmp_path)
        store.save(self._lesson(repo))
        (repo / SOURCE).write_text("print(2)\n")
        assert store.save_or_enrich(self._lesson(repo)) == "enriched"
        assert "changed since learned" not in store.get_context(project_dir=repo)

    def test_unusable_submitted_cites_are_dropped_with_their_commit(self, tmp_path):
        store = self._store(tmp_path)
        store.save(
            Lesson(
                ts="t",
                rule=RULE,
                category="tool",
                cites=[{"path": "../x", "sha256": "c" * 64}],
                cited_commit=COMMIT,
            )
        )
        (loaded,) = store.load_all()
        assert loaded.cites is None and loaded.cited_commit is None


class TestVectorStore:
    def _write(self, store, repo: Path, rule: str = RULE, **fields):
        return store.write_lesson(
            rule,
            "tool",
            None,
            cites=_cite(repo),
            cited_commit=COMMIT,
            **fields,
        )

    def _value(self, store) -> dict:
        (row,) = store.get_lessons()
        return json.loads(row["value_json"])

    def test_a_lesson_without_cites_is_stored_byte_identically(self, tmp_path):
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(RULE, "tool", None)
            assert self._value(store) == {"rule": RULE, "category": "tool", "negative": None}

    def test_cites_and_the_commit_are_stored(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            assert self._write(store, repo)
            value = self._value(store)
            assert value["cites"] == _cite(repo) and value["cited_commit"] == COMMIT

    def test_the_commit_is_not_stored_without_cites(self, tmp_path):
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(RULE, "tool", None, cited_commit=COMMIT)
            assert "cited_commit" not in self._value(store)

    def test_cites_do_not_change_the_lesson_s_identity(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(RULE, "tool", None)
            (before,) = store.get_lessons()
            self._write(store, repo)
            (after,) = store.get_lessons()
            assert before["key"] == after["key"]

    @pytest.mark.parametrize("background", [False, True])
    def test_a_changed_file_adds_the_note(self, tmp_path, background):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            self._write(store, repo)
            (repo / SOURCE).write_text("print(2)\n")
            out = store.get_lessons_context(project_dir=repo, background=background)
            assert f"- {RULE} [cited code changed since learned: {SOURCE}]\n" in out

    @pytest.mark.parametrize("background", [False, True])
    def test_a_current_row_renders_exactly_as_an_uncited_one_does(self, tmp_path, background):
        repo = _repo(tmp_path)
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        cited = _vector_store(tmp_path / "a")
        plain = _vector_store(tmp_path / "b")
        with closing(cited), closing(plain):
            self._write(cited, repo)
            plain.write_lesson(RULE, "tool", None)
            assert cited.get_lessons_context(
                project_dir=repo, background=background
            ) == plain.get_lessons_context(project_dir=repo, background=background)

    @pytest.mark.parametrize("background", [False, True])
    def test_a_gone_file_withholds_the_row_and_reports_it_apart_from_the_budget(
        self, tmp_path, background
    ):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            self._write(store, repo, repo_scope="src/pkg")
            store.write_lesson("prefer tabs over spaces", "preference")
            (repo / SOURCE).unlink()
            out = store.get_lessons_context(project_dir=repo, background=background)
            assert RULE not in out and "prefer tabs over spaces" in out
            assert "[Withheld 1 learned rule: the code it cites no longer exists" in out
            assert "omitted" not in out

    def test_another_checkout_renders_the_row_as_it_always_did(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            self._write(store, repo)
            elsewhere = _repo(tmp_path, "elsewhere", with_commit=False)
            (elsewhere / SOURCE).unlink()
            out = store.get_lessons_context(project_dir=elsewhere)
            assert f"- {RULE}\n" in out and "Withheld" not in out

    def test_a_scoped_row_whose_cite_is_gone_is_withheld_in_its_repository(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            self._write(store, repo, repo_scope="src/pkg")
            (repo / SOURCE).unlink()
            out = store.get_lessons_context(project_dir=repo)
            assert RULE not in out and "Withheld 1" in out

    def test_a_resubmit_with_new_cites_reports_enriched_and_keeps_the_clause(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(RULE, "tool", "calling it twice")
            result = self._write_with_clause(store, repo)
            assert result.outcome is LessonWriteOutcome.ENRICHED
            value = self._value(store)
            assert value["negative"] == "calling it twice"
            assert value["cites"] == _cite(repo)

    def _write_with_clause(self, store, repo):
        return store.write_lesson(RULE, "tool", None, cites=_cite(repo), cited_commit=COMMIT)

    def test_a_resubmit_of_the_same_cites_is_unchanged(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            self._write(store, repo)
            assert self._write(store, repo).outcome is LessonWriteOutcome.UNCHANGED

    def test_a_resubmit_without_cites_never_strips_them(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            self._write(store, repo)
            result = store.write_lesson(RULE, "tool", "a new clause")
            assert result.outcome is LessonWriteOutcome.ENRICHED
            value = self._value(store)
            assert value["cites"] == _cite(repo) and value["cited_commit"] == COMMIT
            assert store.write_lesson(RULE, "tool", "a new clause").outcome is (
                LessonWriteOutcome.UNCHANGED
            )

    def test_cites_upgrade_a_legacy_string_row_in_place(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(RULE, "tool", None)
            (row,) = store.get_lessons()
            store.db.execute(
                "UPDATE semantic_memory SET value_json = ? WHERE key = ?",
                (json.dumps(RULE), row["key"]),
            )
            store.db.commit()
            result = self._write_with_clause(store, repo)
            assert result.outcome is LessonWriteOutcome.ENRICHED
            assert self._value(store)["cites"] == _cite(repo)

    def test_a_near_limit_multibyte_lesson_with_cites_is_still_accepted(self, tmp_path):
        # 1,300 three-byte characters leave room for the cites only when the size
        # check measures the rule's raw bytes plus the cites' raw bytes. Measuring
        # the whole JSON envelope instead would refuse a rule the bare form allows,
        # while a caller with a JSONL fallback reported it saved.
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            rule = "ก" * 1300
            value = {
                "rule": rule,
                "category": "tool",
                "negative": None,
                "cites": _cite(repo),
                "cited_commit": COMMIT,
            }
            assert len(json.dumps(value, ensure_ascii=False).encode()) > 4096
            assert store.validate_semantic("lesson.x", value, 1.0, "user_explicit") is None
            assert self._write(store, repo, rule=rule).wrote

    def test_the_migration_carries_the_cites_across(self, tmp_path, monkeypatch):
        from kiro_crew.vector_memory_runtime import migration

        repo = _repo(tmp_path)
        path = tmp_path / "lessons.jsonl"
        row = {
            "ts": "t",
            "rule": RULE,
            "category": "tool",
            "negative": None,
            "repo_scope": None,
            "cites": _cite(repo),
            "cited_commit": COMMIT,
        }
        path.write_text(json.dumps(row) + "\n")
        (migrated,) = list(migration.legacy_lessons(path))
        assert migrated[5] == _cite(repo) and migrated[6] == COMMIT
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(
                migrated[0],
                migrated[1],
                migrated[2],
                source="migration",
                cites=migrated[5],
                cited_commit=migrated[6],
            )
            assert self._value(store)["cites"] == _cite(repo)


class TestTurnLessons:
    """The per-message block reads the same rows the startup block does."""

    MESSAGE = "the flywheel telemetry canary rollback"

    def _setup(self, tmp_path: Path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        store.write_lesson(
            "roll back the flywheel telemetry canary on regression",
            "tool",
            None,
            repo_scope="src/pkg",
            cites=_cite(repo),
            cited_commit=COMMIT,
        )
        return repo, store

    def _turn(self, store, repo, **overrides):
        arguments = {
            "shown": lambda text: False,
            "project_dir": repo,
            "max_rows": 3,
            "max_chars": 2000,
        }
        return store.turn_lessons(self.MESSAGE, **{**arguments, **overrides})

    def test_a_changed_file_adds_the_note(self, tmp_path):
        repo, store = self._setup(tmp_path)
        with closing(store):
            (repo / SOURCE).write_text("print(2)\n")
            ((_, text),) = self._turn(store, repo)
            assert text.endswith(f" [cited code changed since learned: {SOURCE}]")

    def test_a_gone_file_is_not_offered(self, tmp_path):
        repo, store = self._setup(tmp_path)
        with closing(store):
            (repo / SOURCE).unlink()
            assert self._turn(store, repo) == []

    def test_a_current_row_is_offered_unmarked(self, tmp_path):
        repo, store = self._setup(tmp_path)
        with closing(store):
            ((_, text),) = self._turn(store, repo)
            assert "cited code" not in text

    def test_shown_is_judged_on_the_text_the_startup_block_rendered(self, tmp_path):
        repo, store = self._setup(tmp_path)
        with closing(store):
            (repo / SOURCE).write_text("print(2)\n")
            startup = store.get_lessons_context(project_dir=repo, background=True)
            shown_lines = {line[2:] for line in startup.splitlines() if line.startswith("- ")}
            assert self._turn(store, repo, shown=lambda text: text in shown_lines) == []

    def test_another_checkout_offers_an_unscoped_row_unmarked(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(
                "roll back the flywheel telemetry canary on regression",
                "tool",
                None,
                cites=_cite(repo),
                cited_commit=COMMIT,
            )
            elsewhere = _repo(tmp_path, "elsewhere", with_commit=False)
            (elsewhere / SOURCE).unlink()
            ((_, text),) = self._turn(store, elsewhere)
            assert "cited code" not in text


class TestPromptBuildSpawnsNoSubprocess:
    def test_checking_anchored_rows_runs_no_process(self, tmp_path):
        repo = _repo(tmp_path)
        jsonl = LessonStore(base_dir=tmp_path / "home")
        jsonl.save(Lesson(ts="t", rule=RULE, category="tool", cites=_cite(repo)))
        store = _vector_store(tmp_path)
        store.write_lesson(
            "roll back the flywheel telemetry canary on regression",
            "tool",
            None,
            cites=_cite(repo),
            cited_commit=COMMIT,
        )
        (repo / SOURCE).write_text("print(2)\n")

        def refuse(*args, **kwargs):
            raise AssertionError("a prompt build must not spawn a process")

        with (
            closing(store),
            patch.object(subprocess, "Popen", refuse),
            patch.object(subprocess, "run", refuse),
            patch("os.system", refuse),
        ):
            assert "changed since learned" in jsonl.get_context(project_dir=repo)
            assert "changed since learned" in store.get_lessons_context(project_dir=repo)
            assert store.turn_lessons(
                "the flywheel telemetry canary rollback",
                shown=lambda text: False,
                project_dir=repo,
                max_rows=3,
                max_chars=2000,
            )
            assert read_head_commit(repo) == COMMIT


class TestWriteSurfaces:
    """The route, the tool and the CLI hand cites to the same capture."""

    def _request(self, repo: Path | None, **body):
        request = MagicMock()
        state = MagicMock()
        state.conversation_log = ConversationLog()
        state._background_tasks = set()
        state._slots = {"ui": SimpleNamespace(project=str(repo) if repo else "")}
        request.app = {"state": state}
        request.headers = {"X-Session-Key": "dashboard:ui"}
        attach_body(request, {"rule": RULE, "category": "tool", **body})
        return request, state

    async def _post(self, request, store):
        from kiro_crew.dashboard.handlers import cron

        with (
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(cron, "_sel"),
            patch.object(cron, "_resolve_and_supersede", new=AsyncMock()),
        ):
            resp = await cron.api_lessons_create(request)
        return json.loads(resp.text)

    @pytest.mark.asyncio
    async def test_the_route_records_the_session_projects_files(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            request, _ = self._request(repo, cites=[SOURCE])
            body = await self._post(request, store)
            assert body["ok"] is True and "cites_refused" not in body
            (row,) = store.get_lessons()
            value = json.loads(row["value_json"])
            assert value["cites"] == _cite(repo) and value["cited_commit"] == COMMIT

    @pytest.mark.asyncio
    async def test_the_route_names_a_refused_cite_and_still_saves_the_lesson(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            request, _ = self._request(repo, cites=["src/gone.py", SOURCE])
            body = await self._post(request, store)
            assert body["ok"] is True
            assert [item["path"] for item in body["cites_refused"]] == ["src/gone.py"]
            assert json.loads(store.get_lessons()[0]["value_json"])["cites"] == _cite(repo)

    @pytest.mark.asyncio
    async def test_a_session_with_no_project_saves_the_lesson_and_refuses_the_cite(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            request, _ = self._request(None, cites=[SOURCE])
            body = await self._post(request, store)
            assert body["ok"] is True
            assert "no project" in body["cites_refused"][0]["reason"]
            assert "cites" not in json.loads(store.get_lessons()[0]["value_json"])
            assert repo.exists()

    @pytest.mark.asyncio
    async def test_the_request_cannot_choose_the_project(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            request, _ = self._request(None, cites=[SOURCE], project=str(repo))
            body = await self._post(request, store)
            assert body["cites_refused"], "a body field named project is not a project"

    @pytest.mark.asyncio
    async def test_a_path_in_the_rule_text_is_recorded_without_an_explicit_cite(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            request, _ = self._request(repo, rule=f"never hand edit {SOURCE}")
            await self._post(request, store)
            stored = json.loads(store.get_lessons()[0]["value_json"])["cites"]
            assert stored == [{**_cite(repo)[0], "source": "text"}]

    def test_the_schema_bounds_the_list_and_each_path(self):
        from kiro_crew.validation import LEARN_ADD_SCHEMA, ValidationError, validate_tool_args

        validate_tool_args({"rule": RULE, "cites": [SOURCE]}, LEARN_ADD_SCHEMA)
        with pytest.raises(ValidationError):
            validate_tool_args({"rule": RULE, "cites": ["x.py"] * (CITE_MAX + 1)}, LEARN_ADD_SCHEMA)
        with pytest.raises(ValidationError):
            validate_tool_args({"rule": RULE, "cites": ["x" * 300]}, LEARN_ADD_SCHEMA)

    def test_the_tool_forwards_cites_and_reports_the_refused_ones(self):
        from kiro_crew.mcp_tools import learn as learn_tool

        reply = {
            "ok": True,
            "outcome": "inserted",
            "reason": None,
            "superseded": [],
            "cites_refused": [{"path": "src/gone.py", "reason": "does not resolve"}],
        }
        with (
            patch.object(learn_tool.mcp_core, "_vet_memory_writes_governance", return_value=None),
            patch.object(learn_tool.mcp_core, "_resolve_session_key", return_value="dashboard:ui"),
            patch.object(learn_tool.mcp_core, "_post", return_value=reply) as post,
        ):
            text = learn_tool.learn_add(
                "learn_add", {"rule": RULE, "category": "tool", "cites": [SOURCE]}
            )
        assert post.call_args.args[1]["cites"] == [SOURCE]
        assert text.startswith("Saved lesson")
        assert "src/gone.py: does not resolve" in text

    def test_the_tool_advertises_the_cites_field(self):
        from kiro_crew.mcp_tools import learn as learn_tool

        add = next(t for t in learn_tool.schemas() if t["name"] == "learn_add")
        cites = add["inputSchema"]["properties"]["cites"]
        assert cites["type"] == "array" and cites["maxItems"] == CITE_MAX

    def _cli(self, tmp_path, repo, *cite, monkeypatch):
        import argparse

        from kiro_crew import cli_commands

        store = _vector_store(tmp_path)
        monkeypatch.chdir(repo)
        args = argparse.Namespace(
            learn_action="add", rule=RULE, category="tool", negative=None, cite=list(cite) or None
        )
        with (
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
            # The command closes its store; the test reads the row afterwards.
            patch.object(store, "close"),
        ):
            cli_commands._learn(args)
        return store

    def test_the_cli_records_a_repeatable_cite_from_the_current_repository(
        self, tmp_path, monkeypatch
    ):
        repo = _repo(tmp_path)
        store = self._cli(tmp_path, repo, SOURCE, monkeypatch=monkeypatch)
        with closing(store):
            value = json.loads(store.get_lessons()[0]["value_json"])
            assert value["cites"] == _cite(repo)

    def test_the_cli_names_a_refused_cite_on_stderr_and_still_saves(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = _repo(tmp_path)
        store = self._cli(tmp_path, repo, "src/gone.py", monkeypatch=monkeypatch)
        with closing(store):
            assert "cites" not in json.loads(store.get_lessons()[0]["value_json"])
        captured = capsys.readouterr()
        assert "Cite not recorded: src/gone.py" in captured.err
        assert captured.out.startswith("Saved:")

    def test_the_cli_advertises_a_repeatable_cite_flag(self, tmp_path):
        import os
        import sys

        out = subprocess.run(
            [sys.executable, "-m", "kiro_crew", "learn", "add", "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env={**os.environ, "KIROCREW_HOME": str(tmp_path)},
            check=True,
        ).stdout
        assert "--cite PATH" in out


def _loose_object(repo: Path, commit: str) -> None:
    path = repo / ".git" / "objects" / commit[:2]
    path.mkdir(parents=True, exist_ok=True)
    (path / commit[2:]).write_bytes(b"x")


def _pack_index(repo: Path, names: list[str], *, name: str = "pack-a.idx") -> None:
    """A minimal version-2 pack index listing *names* (hex object names)."""
    import struct

    raw = sorted(bytes.fromhex(n) for n in names)
    fanout = [sum(1 for r in raw if r[0] <= i) for i in range(256)]
    pack = repo / ".git" / "objects" / "pack"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / name).write_bytes(
        b"\xfftOc\x00\x00\x00\x02" + struct.pack(">256I", *fanout) + b"".join(raw)
    )


class TestCommitEstablishesTheRepository:
    """A deleted file withholds a row that has no scope and no cite left, but only in
    the repository the lesson was written in."""

    def _anchored(self, repo: Path) -> tuple[list[dict[str, str]], str]:
        return _cite(repo), COMMIT

    def test_a_loose_commit_object_is_found(self, tmp_path):
        repo = _repo(tmp_path)
        _loose_object(repo, COMMIT)
        assert commit_in_repo(repo, COMMIT) is True
        assert commit_in_repo(repo, OTHER_COMMIT) is False

    def test_a_packed_commit_object_is_found(self, tmp_path):
        repo = _repo(tmp_path)
        _pack_index(repo, [COMMIT, "c" * 40, "0" * 40, "f" * 40])
        assert commit_in_repo(repo, COMMIT) is True
        assert commit_in_repo(repo, "c" * 40) is True
        assert commit_in_repo(repo, OTHER_COMMIT) is False

    def test_a_worktree_reads_the_common_object_store(self, tmp_path):
        main = _repo(tmp_path, "main")
        _loose_object(main, COMMIT)
        admin = main / ".git" / "worktrees" / "wt"
        admin.mkdir(parents=True)
        (admin / "HEAD").write_text(COMMIT + "\n")
        (admin / "commondir").write_text("../..\n")
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {admin}\n")
        assert commit_in_repo(wt, COMMIT) is True

    @pytest.mark.parametrize(
        "content",
        [b"", b"short", b"\xfftOc\x00\x00\x00\x02" + b"\x00" * 100, b"not an index" * 200],
    )
    def test_a_malformed_pack_index_answers_false(self, tmp_path, content):
        repo = _repo(tmp_path)
        pack = repo / ".git" / "objects" / "pack"
        pack.mkdir(parents=True)
        (pack / "pack-bad.idx").write_bytes(content)
        assert commit_in_repo(repo, OTHER_COMMIT) is False

    def test_a_fanout_that_overruns_the_file_answers_false(self, tmp_path):
        import struct

        repo = _repo(tmp_path)
        pack = repo / ".git" / "objects" / "pack"
        pack.mkdir(parents=True)
        (pack / "pack-lie.idx").write_bytes(
            b"\xfftOc\x00\x00\x00\x02" + struct.pack(">256I", *([10**9] * 256))
        )
        assert commit_in_repo(repo, OTHER_COMMIT) is False

    def test_not_a_commit_name_or_not_a_repository_answers_false(self, tmp_path):
        assert commit_in_repo(_repo(tmp_path), "HEAD") is False
        assert commit_in_repo(None, COMMIT) is False
        assert commit_in_repo(tmp_path / "nowhere", COMMIT) is False

    def test_a_deleted_only_cite_withholds_the_row_in_its_own_repository(self, tmp_path):
        repo = _repo(tmp_path)
        _loose_object(repo, COMMIT)
        cites, commit = self._anchored(repo)
        (repo / SOURCE).unlink()
        review = CiteReview(repo)
        assert review.annotate(cites, scope_satisfied=False, cited_commit=commit) == (False, "")
        assert review.withheld == 1

    def test_another_checkout_without_the_commit_still_renders_the_row(self, tmp_path):
        repo = _repo(tmp_path)
        cites, commit = self._anchored(repo)
        elsewhere = _repo(tmp_path, "elsewhere", with_commit=False)
        (elsewhere / SOURCE).unlink()
        _loose_object(elsewhere, OTHER_COMMIT)
        review = CiteReview(elsewhere)
        assert review.annotate(cites, scope_satisfied=False, cited_commit=commit) == (True, "")
        assert review.withheld == 0

    def test_the_commit_is_looked_up_once_per_prompt_build(self, tmp_path):
        repo = _repo(tmp_path)
        cites, commit = self._anchored(repo)
        (repo / SOURCE).unlink()
        review = CiteReview(repo)
        with patch("kiro_crew.lesson_cites.commit_in_repo", return_value=False) as lookup:
            review.annotate(cites, scope_satisfied=False, cited_commit=commit)
            review.annotate(cites, scope_satisfied=False, cited_commit=commit)
        assert lookup.call_count == 1

    def test_jsonl_store_withholds_an_unscoped_single_cite_row_when_its_file_is_gone(
        self, tmp_path
    ):
        repo = _repo(tmp_path)
        _loose_object(repo, COMMIT)
        store = LessonStore(base_dir=tmp_path / "home")
        store.save(
            Lesson(ts="t", rule=RULE, category="tool", cites=_cite(repo), cited_commit=COMMIT)
        )
        (repo / SOURCE).unlink()
        assert store.get_context(project_dir=repo).startswith("[Withheld 1 learned rule")

    @pytest.mark.parametrize("background", [False, True])
    def test_vector_store_withholds_an_unscoped_single_cite_row_when_its_file_is_gone(
        self, tmp_path, background
    ):
        repo = _repo(tmp_path)
        _loose_object(repo, COMMIT)
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(RULE, "tool", None, cites=_cite(repo), cited_commit=COMMIT)
            (repo / SOURCE).unlink()
            out = store.get_lessons_context(project_dir=repo, background=background)
            assert RULE not in out and "[Withheld 1 learned rule" in out


class TestACoincidentPathDoesNotEstablishARepository:
    """``README.md`` exists in most repositories. With a commit on record, only the
    commit says a session is in the repository the lesson was written in."""

    def _anchored(self, repo: Path) -> list[dict[str, str]]:
        (repo / "README.md").write_text("# readme\n")
        cites = capture_cites(["README.md", SOURCE], [], repo).cites
        assert cites and len(cites) == 2
        return cites

    def _other_checkout(self, tmp_path: Path) -> Path:
        other = _repo(tmp_path, "other", with_commit=False)
        (other / SOURCE).unlink()
        (other / "README.md").write_text("# a different readme\n")
        return other

    def test_review_keeps_the_row_unmarked_in_another_checkout(self, tmp_path):
        repo = _repo(tmp_path)
        cites = self._anchored(repo)
        review = CiteReview(self._other_checkout(tmp_path))
        assert review.annotate(cites, scope_satisfied=False, cited_commit=COMMIT) == (True, "")
        assert review.withheld == 0

    def test_both_stores_render_the_row_untouched_in_another_checkout(self, tmp_path):
        repo = _repo(tmp_path)
        cites = self._anchored(repo)
        other = self._other_checkout(tmp_path)
        jsonl = LessonStore(base_dir=tmp_path / "home")
        jsonl.save(Lesson(ts="t", rule=RULE, category="tool", cites=cites, cited_commit=COMMIT))
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(RULE, "tool", None, cites=cites, cited_commit=COMMIT)
            for out in (
                jsonl.get_context(project_dir=other),
                store.get_lessons_context(project_dir=other),
            ):
                assert f"- {RULE}\n" in out
                assert "Withheld" not in out and "cited code changed" not in out

    def test_without_a_commit_one_resolving_cite_still_establishes(self, tmp_path):
        repo = _repo(tmp_path)
        cites = self._anchored(repo)
        other = self._other_checkout(tmp_path)
        assert CiteReview(other).annotate(cites, scope_satisfied=False) == (False, "")


class TestGuardedPackIndexReads:
    def test_a_non_regular_file_named_like_a_pack_index_is_refused_not_read(self, tmp_path):
        # A directory is the platform-neutral stand-in for any non-regular file (a FIFO
        # on POSIX): the guarded reader opens regular files only.
        repo = _repo(tmp_path)
        pack = repo / ".git" / "objects" / "pack"
        (pack / "pack-dir.idx").mkdir(parents=True)
        assert commit_in_repo(repo, OTHER_COMMIT) is False

    @requires_symlinks
    def test_a_pack_index_symlinked_to_a_sensitive_file_is_not_read(self, tmp_path, monkeypatch):
        from kiro_crew import hooks

        repo = _repo(tmp_path)
        secret = tmp_path / "pretend_home" / ".ssh"
        secret.mkdir(parents=True)
        (secret / "id_rsa").write_bytes(b"\xfftOc\x00\x00\x00\x02" + b"\x00" * 1100)
        pack = repo / ".git" / "objects" / "pack"
        pack.mkdir(parents=True)
        (pack / "pack-link.idx").symlink_to(secret / "id_rsa")
        monkeypatch.setattr(
            hooks, "is_sensitive_path", lambda p, base_dir=None: ".ssh" in Path(p).parts
        )
        with patch.object(hooks, "safe_read_range", wraps=hooks.safe_read_range) as read:
            assert commit_in_repo(repo, OTHER_COMMIT) is False
        assert read.call_count == 0, "a linked index is refused before it is opened"

    def test_safe_read_range_reads_a_window_and_stops_at_the_end(self, tmp_path):
        from kiro_crew.hooks import safe_read_range

        f = tmp_path / "f.bin"
        f.write_bytes(b"0123456789")
        assert safe_read_range(str(f), 2, 4) == b"2345"
        assert safe_read_range(str(f), 8, 4) == b"89"
        assert safe_read_range(str(f), 20, 4) == b""
        assert safe_read_range(str(f), -1, 4) is None
        assert safe_read_range(str(f), 0, 0) == b""
        assert safe_read_range(str(tmp_path / "missing"), 0, 4) is None

    def test_safe_read_range_refuses_an_offset_it_cannot_seek_to(self, tmp_path):
        from kiro_crew.hooks import safe_read_range

        f = tmp_path / "f.bin"
        f.write_bytes(b"0123456789")
        assert safe_read_range(str(f), 2**70, 4) is None


class TestTheWithheldLineStaysInsideTheCap:
    def _gone(self, tmp_path: Path):
        repo = _repo(tmp_path)
        (repo / "src" / "pkg" / "other.py").write_text("x = 1\n")
        gone = capture_cites(["src/pkg/other.py"], [], repo).cites
        (repo / "src" / "pkg" / "other.py").unlink()
        return repo, gone

    def test_the_vector_block_is_not_longer_than_its_cap(self, tmp_path):
        repo, gone = self._gone(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson("prefer tabs over spaces in makefiles", "preference")
            store.write_lesson(
                "bump the manifest version on release",
                "tool",
                None,
                repo_scope="src/pkg",
                cites=gone,
                cited_commit=COMMIT,
            )
            full = store.get_lessons_context(project_dir=repo)
            assert "[Withheld 1 learned rule" in full
            cap = len(full) + 5
            assert len(store.get_lessons_context(project_dir=repo, cap=cap)) <= cap

    def test_the_jsonl_block_is_not_longer_than_its_cap(self, tmp_path):
        repo, gone = self._gone(tmp_path)
        store = LessonStore(base_dir=tmp_path / "home")
        store.save(Lesson(ts="t", rule="prefer tabs over spaces in makefiles", category="tool"))
        store.save(
            Lesson(
                ts="t",
                rule="bump the manifest version",
                category="tool",
                repo_scope="src/pkg",
                cites=gone,
                cited_commit=COMMIT,
            )
        )
        full = store.get_context(project_dir=repo)
        assert "[Withheld 1 learned rule" in full
        cap = len(full) + 5
        assert len(store.get_context(project_dir=repo, cap=cap)) <= cap

    def test_a_notice_that_cannot_fit_is_dropped_rather_than_overrunning(self):
        from kiro_crew.lesson_cites import fit_withheld, withheld_notice

        notice = withheld_notice(1)
        assert fit_withheld(notice, 50, 0) == ("", (50, 0))
        kept, (cap, unbounded) = fit_withheld(notice, len(notice) + 100, 0)
        assert kept == notice and cap == 100 and unbounded == 0
        assert fit_withheld("", 10) == ("", (10,))


class TestWriteSurfacesSayWhatWasRecorded:
    def _request(self, repo: Path, **body):
        request = MagicMock()
        state = MagicMock()
        state.conversation_log = ConversationLog()
        state._background_tasks = set()
        state._slots = {"ui": SimpleNamespace(project=str(repo))}
        request.app = {"state": state}
        request.headers = {"X-Session-Key": "dashboard:ui"}
        attach_body(request, {"rule": RULE, "category": "tool", **body})
        return request

    async def _post(self, request, store):
        from kiro_crew.dashboard.handlers import cron

        with (
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=store)),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(cron, "_sel"),
            patch.object(cron, "_resolve_and_supersede", new=AsyncMock()),
        ):
            resp = await cron.api_lessons_create(request)
        return json.loads(resp.text)

    @pytest.mark.asyncio
    async def test_a_cite_taken_from_the_rule_text_is_named_in_the_response(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            body = await self._post(self._request(repo, rule=f"never hand edit {SOURCE}"), store)
            assert body["cites_from_text"] == [SOURCE]

    @pytest.mark.asyncio
    async def test_an_explicit_cite_is_not_reported_as_taken_from_the_text(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            body = await self._post(self._request(repo, cites=[SOURCE]), store)
            assert "cites_from_text" not in body

    @pytest.mark.asyncio
    async def test_a_refused_path_is_redacted_before_it_leaves_the_route(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        secret = "AKIAIOSFODNN7EXAMPLE"
        with closing(store):
            body = await self._post(self._request(repo, cites=[f"{secret}/leak.py"]), store)
            assert body["ok"] is True
            assert secret not in json.dumps(body)
            assert len(body["cites_refused"]) == 1

    def test_the_tool_tells_the_writer_which_files_the_text_anchored(self):
        from kiro_crew.mcp_tools import learn as learn_tool

        reply = {
            "ok": True,
            "outcome": "inserted",
            "reason": None,
            "superseded": [],
            "cites_from_text": [SOURCE],
        }
        with (
            patch.object(learn_tool.mcp_core, "_vet_memory_writes_governance", return_value=None),
            patch.object(learn_tool.mcp_core, "_resolve_session_key", return_value="dashboard:ui"),
            patch.object(learn_tool.mcp_core, "_post", return_value=reply),
        ):
            text = learn_tool.learn_add("learn_add", {"rule": RULE, "category": "tool"})
        assert text.startswith("Saved lesson")
        assert f"because the rule's text names them: {SOURCE}" in text

    def test_the_tool_does_not_claim_a_clause_when_only_cites_changed(self):
        from kiro_crew.mcp_tools import learn as learn_tool

        reply = {"ok": True, "outcome": "enriched", "reason": None, "superseded": []}
        with (
            patch.object(learn_tool.mcp_core, "_vet_memory_writes_governance", return_value=None),
            patch.object(learn_tool.mcp_core, "_resolve_session_key", return_value="dashboard:ui"),
            patch.object(learn_tool.mcp_core, "_post", return_value=reply),
        ):
            text = learn_tool.learn_add(
                "learn_add", {"rule": RULE, "category": "tool", "negative": "x", "cites": [SOURCE]}
            )
        assert "with the new clause or cited files" in text

    def test_the_cli_names_the_files_the_rule_text_anchored(self, tmp_path, monkeypatch, capsys):
        import argparse

        from kiro_crew import cli_commands

        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        monkeypatch.chdir(repo)
        args = argparse.Namespace(
            learn_action="add", rule=f"never hand edit {SOURCE}", category="tool", negative=None
        )
        with (
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
            patch.object(store, "close"),
        ):
            cli_commands._learn(args)
        with closing(store):
            assert f"because the rule's text names them: {SOURCE}" in capsys.readouterr().err


class TestTextDerivedCitesOnlyAnnotate:
    """A path in the rule's prose is often an example, so it never withholds the rule."""

    def _text_cites(self, repo: Path) -> list[dict[str, str]]:
        cites = capture_cites([], [f"never hand edit {SOURCE}"], repo).cites
        assert cites and cites[0]["source"] == "text"
        return cites

    def test_a_text_derived_cite_is_marked_as_such(self, tmp_path):
        repo = _repo(tmp_path)
        assert self._text_cites(repo) == [{**_cite(repo)[0], "source": "text"}]

    def test_an_explicit_cite_carries_no_source(self, tmp_path):
        assert "source" not in _cite(_repo(tmp_path))[0]

    def test_the_source_survives_normalization_and_nothing_else_does(self):
        raw = [
            {"path": SOURCE, "sha256": "c" * 64, "source": "text"},
            {"path": "src/b.py", "sha256": "c" * 64, "source": "anything else"},
        ]
        assert normalize_cites(raw) == [
            {"path": SOURCE, "sha256": "c" * 64, "source": "text"},
            {"path": "src/b.py", "sha256": "c" * 64},
        ]

    def test_a_gone_text_derived_cite_annotates_and_does_not_withhold(self, tmp_path):
        repo = _repo(tmp_path)
        cites = self._text_cites(repo)
        (repo / SOURCE).unlink()
        review = CiteReview(repo)
        keep, note = review.annotate(cites, scope_satisfied=True, cited_commit=COMMIT)
        assert keep is True and SOURCE in note
        assert review.withheld == 0

    def test_a_gone_explicit_cite_still_withholds(self, tmp_path):
        repo = _repo(tmp_path)
        cites = _cite(repo)
        (repo / SOURCE).unlink()
        assert CiteReview(repo).annotate(cites, scope_satisfied=True) == (False, "")

    def test_a_mixed_row_withholds_only_for_the_explicit_cite(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "src" / "b.py").write_text("b = 1\n")
        cites = capture_cites(["src/b.py"], [f"see {SOURCE}"], repo).cites
        (repo / SOURCE).unlink()
        assert CiteReview(repo).annotate(cites, scope_satisfied=True)[0] is True
        (repo / "src" / "b.py").unlink()
        assert CiteReview(repo).annotate(cites, scope_satisfied=True)[0] is False

    def test_the_stores_keep_the_source_through_a_round_trip(self, tmp_path):
        repo = _repo(tmp_path)
        cites = self._text_cites(repo)
        jsonl = LessonStore(base_dir=tmp_path / "home")
        jsonl.save(Lesson(ts="t", rule=RULE, category="tool", cites=cites, cited_commit=COMMIT))
        assert jsonl.load_all()[0].cites == cites
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(RULE, "tool", None, cites=cites, cited_commit=COMMIT)
            assert json.loads(store.get_lessons()[0]["value_json"])["cites"] == cites


class TestAFileThatCannotBeHashedIsFlagged:
    def test_a_cited_file_that_grew_past_the_bound_is_not_current(self, tmp_path):
        repo = _repo(tmp_path)
        cites = _cite(repo)
        (repo / SOURCE).write_bytes(b"x" * ((1 << 20) + 1))
        keep, note = CiteReview(repo).annotate(cites, scope_satisfied=False, cited_commit=COMMIT)
        assert keep is True and note == f" [cited code changed since learned: {SOURCE}]"


class TestTextCitesAreRedactedBeforeTheyLeaveTheRoute:
    @pytest.mark.asyncio
    async def test_a_credential_shaped_file_name_in_the_rule_is_masked(self, tmp_path):
        secret = "AKIAIOSFODNN7EXAMPLE"
        repo = _repo(tmp_path)
        (repo / "src" / f"{secret}.py").write_text("x = 1\n")
        helper = TestWriteSurfacesSayWhatWasRecorded()
        store = _vector_store(tmp_path)
        with closing(store):
            request = helper._request(repo, rule=f"never hand edit src/{secret}.py")
            body = await helper._post(request, store)
            assert body["cites_from_text"], "the file was recorded, so the writer is told"
            assert secret not in json.dumps(body)


class TestCitedFileReadsAreAuthorized:
    def test_a_denied_read_is_refused_like_a_missing_path_and_never_hashed(self, tmp_path):
        repo = _repo(tmp_path)
        asked: list[Path] = []

        def deny(path: Path) -> bool:
            asked.append(path)
            return False

        with patch("kiro_crew.lesson_cites._file_digest") as digest:
            denied = capture_cites([SOURCE], [], repo, may_read=deny)
            missing = capture_cites(["src/gone.py"], [], repo)
        digest.assert_not_called()
        assert denied.cites is None and asked == [(repo / SOURCE).resolve()]
        assert denied.refused[0][1] == missing.refused[0][1]

    def test_an_allowed_read_records_the_cite(self, tmp_path):
        repo = _repo(tmp_path)
        assert capture_cites([SOURCE], [], repo, may_read=lambda path: True).cites

    def test_a_denied_text_mention_is_ignored_without_a_refusal(self, tmp_path):
        repo = _repo(tmp_path)
        captured = capture_cites([], [f"edit {SOURCE}"], repo, may_read=lambda path: False)
        assert captured.cites is None and captured.refused == () and captured.from_text == ()

    @pytest.mark.asyncio
    async def test_the_route_asks_the_sessions_filesystem_read_scope(self, tmp_path):
        from kiro_crew.platform import governance_profiles

        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        asked: list[tuple[str, str, str]] = []

        def verdict(scope, item, **kwargs):
            asked.append((scope, item, kwargs.get("session_key", "")))
            return SimpleNamespace(permitted=False, reason="narrowed")

        helper = TestWriteSurfacesSayWhatWasRecorded()
        with closing(store), patch.object(governance_profiles, "governance_permits", verdict):
            body = await helper._post(helper._request(repo, cites=[SOURCE]), store)
            assert body["ok"] is True and len(body["cites_refused"]) == 1
            assert "cites" not in json.loads(store.get_lessons()[0]["value_json"])
        assert asked == [("filesystem.read", str((repo / SOURCE).resolve()), "dashboard:ui")]

    @pytest.mark.asyncio
    async def test_the_route_denies_when_the_governance_check_errors(self, tmp_path):
        from kiro_crew.platform import governance_profiles

        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        seen: dict[str, object] = {}

        def verdict(scope, item, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(permitted=True)

        helper = TestWriteSurfacesSayWhatWasRecorded()
        with closing(store), patch.object(governance_profiles, "governance_permits", verdict):
            await helper._post(helper._request(repo, cites=[SOURCE]), store)
        assert seen["fail_closed"] is True


class TestFromTextIsReportedOnlyWhenTheWriteLanded:
    @pytest.mark.asyncio
    async def test_a_refused_write_reports_no_recorded_cites(self, tmp_path):
        from kiro_crew.dashboard.handlers import cron
        from kiro_crew.vector_memory import LessonWriteResult

        repo = _repo(tmp_path)
        helper = TestWriteSurfacesSayWhatWasRecorded()
        vs = MagicMock()
        vs.embed_lesson.return_value = [0.1] * 4
        vs.write_lesson.return_value = LessonWriteResult(LessonWriteOutcome.REFUSED, "x")
        with (
            patch.object(cron, "_get_memory", return_value=MagicMock(vector_store=vs)),
            patch.object(cron, "_is_restricted_session", return_value=False),
            patch.object(cron, "_sel"),
        ):
            resp = await cron.api_lessons_create(
                helper._request(repo, rule=f"never hand edit {SOURCE}")
            )
        assert "cites_from_text" not in json.loads(resp.text)

    def test_the_cli_prints_nothing_when_the_lesson_was_declined(
        self, tmp_path, monkeypatch, capsys
    ):
        import argparse

        from kiro_crew import cli_commands
        from kiro_crew.vector_memory import LessonWriteResult

        repo = _repo(tmp_path)
        monkeypatch.chdir(repo)
        store = MagicMock()
        store.write_lesson.return_value = LessonWriteResult(LessonWriteOutcome.DEDUPED, "x")
        args = argparse.Namespace(
            learn_action="add", rule=f"never hand edit {SOURCE}", category="tool", negative=None
        )
        with (
            patch.object(cli_commands, "VectorMemoryStore", return_value=store),
            patch.object(cli_commands, "LessonStore", return_value=MagicMock()),
            patch.object(cli_commands.KiroCrewConfig, "load", return_value=MagicMock()),
        ):
            cli_commands._learn(args)
        assert "Also recorded" not in capsys.readouterr().err


class TestRootLevelFilesInTheRuleText:
    def test_a_root_level_file_named_in_the_rule_is_recorded(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "README.md").write_text("# readme\n")
        captured = capture_cites([], ["keep README.md in sync with the CLI"], repo)
        assert [c["path"] for c in captured.cites or []] == ["README.md"]
        assert captured.from_text == ("README.md",)

    def test_a_version_number_or_abbreviation_is_not_a_cite(self, tmp_path):
        repo = _repo(tmp_path)
        assert (
            capture_cites([], ["upgrade to v1.2.3, i.e. the new one, e.g. 2.0"], repo).cites is None
        )


class TestRecheckingIsBoundedPerBuild:
    def test_files_past_the_budget_are_left_unchecked(self, tmp_path, monkeypatch):
        from kiro_crew import lesson_cites

        monkeypatch.setattr(lesson_cites, "FILES_PER_BUILD", 2)
        repo = _repo(tmp_path)
        rows = []
        for i in range(3):
            (repo / "src" / f"f{i}.py").write_text("a")
            rows.append(capture_cites([f"src/f{i}.py"], [], repo).cites)
        for i in range(3):
            (repo / "src" / f"f{i}.py").write_text("b")
        review = CiteReview(repo)
        notes = [review.annotate(r, scope_satisfied=True, cited_commit=COMMIT)[1] for r in rows]
        assert [bool(n) for n in notes] == [True, True, False]

    def test_a_file_two_rows_share_costs_one_unit_of_the_budget(self, tmp_path, monkeypatch):
        from kiro_crew import lesson_cites

        monkeypatch.setattr(lesson_cites, "FILES_PER_BUILD", 1)
        repo = _repo(tmp_path)
        cites = _cite(repo)
        (repo / SOURCE).write_text("changed\n")
        review = CiteReview(repo)
        assert all(
            review.annotate(cites, scope_satisfied=True, cited_commit=COMMIT)[1] for _ in range(3)
        )

    def test_a_missing_file_costs_a_unit_like_any_other_path(self, tmp_path, monkeypatch):
        from kiro_crew import lesson_cites

        monkeypatch.setattr(lesson_cites, "FILES_PER_BUILD", 1)
        repo = _repo(tmp_path)
        (repo / "src" / "gone.py").write_text("x")
        gone = capture_cites(["src/gone.py"], [], repo).cites
        (repo / "src" / "gone.py").unlink()
        cites = _cite(repo)
        (repo / SOURCE).write_text("changed\n")
        review = CiteReview(repo, may_read=lambda path: True)
        assert review.annotate(gone, scope_satisfied=True) == (False, "")
        assert review.annotate(cites, scope_satisfied=True, cited_commit=COMMIT) == (True, "")

    def test_a_build_past_the_budget_asks_governance_about_nothing_further(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew import lesson_cites

        monkeypatch.setattr(lesson_cites, "FILES_PER_BUILD", 2)
        repo = _repo(tmp_path)
        asked: list[Path] = []

        def allow(path: Path) -> bool:
            asked.append(path)
            return True

        review = CiteReview(repo, may_read=allow)
        for i in range(10):
            row = [{"path": f"src/p{i}.py", "sha256": "c" * 64}]
            review.annotate(row, scope_satisfied=True)
        assert len(asked) == 2


def test_one_constant_bounds_a_cites_path_everywhere():
    from kiro_crew.lesson_cites import CITE_PATH_MAX
    from kiro_crew.mcp_tools import learn as learn_tool
    from kiro_crew.validation import LEARN_ADD_SCHEMA

    field = next(f for f in LEARN_ADD_SCHEMA.fields if f.name == "cites")
    add = next(t for t in learn_tool.schemas() if t["name"] == "learn_add")
    assert field.item_max_len == CITE_PATH_MAX
    assert add["inputSchema"]["properties"]["cites"]["items"]["maxLength"] == CITE_PATH_MAX
    over = [{"path": "a/" * (CITE_PATH_MAX // 2) + "b.py", "sha256": "c" * 64}]
    assert normalize_cites(over) is None


class TestRechecksAskGovernanceForTheRenderedSession:
    def _anchored_store(self, tmp_path: Path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        store.write_lesson(RULE, "tool", None, cites=_cite(repo), cited_commit=COMMIT)
        (repo / SOURCE).write_text("changed\n")
        return repo, store

    def test_the_session_named_for_the_render_reaches_governance(self, tmp_path):
        from kiro_crew.lesson_cites import cite_session
        from kiro_crew.platform import governance_profiles

        repo, store = self._anchored_store(tmp_path)
        asked: list[tuple[str, str]] = []

        def verdict(scope, item, **kwargs):
            asked.append((scope, kwargs.get("session_key", "?")))
            return SimpleNamespace(permitted=True)

        with closing(store), patch.object(governance_profiles, "governance_permits", verdict):
            with cite_session("sess-42"):
                store.get_lessons_context(project_dir=repo)
        assert asked and {key for _, key in asked} == {"sess-42"}

    def test_no_session_named_means_no_profile_to_ask_so_nothing_is_read(self, tmp_path):
        from kiro_crew.lesson_cites import cite_session
        from kiro_crew.platform import governance_profiles

        repo, store = self._anchored_store(tmp_path)
        with (
            closing(store),
            patch.object(governance_profiles, "governance_permits") as asked,
            patch("kiro_crew.lesson_cites._file_digest") as digest,
            cite_session(""),
        ):
            out = store.get_lessons_context(project_dir=repo)
        asked.assert_not_called()
        digest.assert_not_called()
        assert f"- {RULE}\n" in out and "cited code changed" not in out

    def test_a_worker_names_the_session_itself(self):
        from concurrent.futures import ThreadPoolExecutor

        from kiro_crew.lesson_cites import _CITE_SESSION, run_for_session

        with ThreadPoolExecutor(max_workers=1) as pool:
            seen = pool.submit(run_for_session, "sess-9", lambda: _CITE_SESSION.get()).result()
            after = pool.submit(lambda: _CITE_SESSION.get()).result()
        assert seen == "sess-9" and after == ""

    def test_a_denied_recheck_leaves_the_row_unchecked_and_unread(self, tmp_path):
        from kiro_crew.platform import governance_profiles

        repo, store = self._anchored_store(tmp_path)
        denied = SimpleNamespace(permitted=False, reason="narrowed")
        with (
            closing(store),
            patch.object(governance_profiles, "governance_permits", return_value=denied),
            patch("kiro_crew.lesson_cites._file_digest") as digest,
        ):
            out = store.get_lessons_context(project_dir=repo)
        digest.assert_not_called()
        assert f"- {RULE}\n" in out and "cited code changed" not in out

    def test_a_denied_file_is_asked_once_per_build(self, tmp_path):
        repo = _repo(tmp_path)
        cites = _cite(repo)
        asked: list[Path] = []

        def deny(path: Path) -> bool:
            asked.append(path)
            return False

        review = CiteReview(repo, may_read=deny)
        for _ in range(3):
            assert review.annotate(cites, scope_satisfied=True) == (True, "")
        assert len(asked) == 1

    def test_the_write_is_audited_through_the_governance_seam(self, tmp_path):
        from kiro_crew.lesson_cites import governed_may_read

        repo = _repo(tmp_path)
        with patch("kiro_crew.sel.sel") as sel:
            allowed = governed_may_read("dashboard:ui")(repo / SOURCE)
        assert allowed is True
        record = sel.return_value.log_governance_decision.call_args.kwargs
        assert record["scope"] == "filesystem.read" and record["outcome"] == "allowed"
        assert record["session_key"] == "dashboard:ui" and record["tool_name"] == "learn_add"


class TestACitesOnlyResubmitOfALegacyRowKeepsItsClause:
    def test_the_in_band_clause_is_carried_into_the_upgraded_row(self, tmp_path):
        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(RULE, "tool", "calling it twice")
            (row,) = store.get_lessons()
            store.db.execute(
                "UPDATE semantic_memory SET value_json = ? WHERE key = ?",
                (json.dumps(f"{RULE} — NOT: calling it twice"), row["key"]),
            )
            store.db.commit()
            result = store.write_lesson(RULE, "tool", None, cites=_cite(repo), cited_commit=COMMIT)
            assert result.outcome is LessonWriteOutcome.ENRICHED
            (stored,) = store.get_lessons()
            value = json.loads(stored["value_json"])
            assert value["negative"] == "calling it twice" and value["cites"] == _cite(repo)


class TestADenialIsNotAnExistenceOracle:
    def test_a_denied_path_answers_the_same_whether_or_not_the_file_exists(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "src" / "other.py").write_text("x = 1\n")
        cites = capture_cites([SOURCE, "src/other.py"], [], repo).cites
        (repo / "src" / "other.py").unlink()
        review = CiteReview(repo, may_read=lambda path: False)
        assert review.annotate(cites, scope_satisfied=True) == (True, "")
        assert review.withheld == 0

    def test_a_missing_file_is_still_withheld_when_reads_are_allowed(self, tmp_path):
        repo = _repo(tmp_path)
        cites = _cite(repo)
        (repo / SOURCE).unlink()
        assert CiteReview(repo, may_read=lambda path: True).annotate(
            cites, scope_satisfied=True
        ) == (
            False,
            "",
        )

    def test_the_read_is_authorized_before_existence_is_probed(self, tmp_path):
        repo = _repo(tmp_path)
        order: list[str] = []

        def deny(path: Path) -> bool:
            order.append("authorize")
            return False

        with patch("kiro_crew.lesson_cites.resolve_in_project") as probe:
            probe.side_effect = lambda *a, **k: order.append("probe")
            CiteReview(repo, may_read=deny).annotate(_cite_for(repo), scope_satisfied=True)
        assert order == ["authorize"]

    def test_write_time_asks_before_probing_a_path_that_does_not_exist(self, tmp_path):
        repo = _repo(tmp_path)
        asked: list[Path] = []

        def deny(path: Path) -> bool:
            asked.append(path)
            return False

        captured = capture_cites(["src/never_existed.py"], [], repo, may_read=deny)
        assert len(asked) == 1 and captured.cites is None


def _cite_for(repo: Path) -> list[dict[str, str]]:
    return [{"path": SOURCE, "sha256": "c" * 64}]


class TestAReSubmitOfTheRuleNeverLosesAnExplicitCite:
    """A rule whose text names a file is re-submitted without ``cites``; that must not
    replace, re-hash or demote what the writer stated."""

    def _two(self, repo: Path) -> list[dict[str, str]]:
        (repo / "src" / "b.py").write_text("b = 1\n")
        cites = capture_cites([SOURCE, "src/b.py"], [], repo).cites
        assert cites and len(cites) == 2
        return cites

    def _text_only(self, repo: Path, text: str) -> tuple[list[dict[str, str]], str | None]:
        captured = capture_cites([], [text], repo)
        assert captured.cites
        return captured.cites, captured.cited_commit

    def test_reconcile_keeps_the_stored_set_for_a_text_only_submission(self, tmp_path):
        from kiro_crew.lesson_cites import reconcile_cites, reconcile_commit

        repo = _repo(tmp_path)
        stored = self._two(repo)
        (repo / SOURCE).write_text("edited\n")
        text, commit = self._text_only(repo, f"never edit {SOURCE}")
        assert reconcile_cites(stored, text) == stored
        assert reconcile_commit(COMMIT, text, OTHER_COMMIT) == COMMIT

    def test_reconcile_adds_a_new_text_path_and_replaces_for_an_explicit_one(self, tmp_path):
        from kiro_crew.lesson_cites import reconcile_cites, reconcile_commit

        repo = _repo(tmp_path)
        (repo / "src" / "c.py").write_text("c = 1\n")
        stored = _cite(repo)
        text, _ = self._text_only(repo, "see src/c.py")
        assert [c["path"] for c in reconcile_cites(stored, text) or []] == [SOURCE, "src/c.py"]
        explicit = capture_cites(["src/c.py"], [], repo).cites
        assert reconcile_cites(stored, explicit) == explicit
        assert reconcile_commit(COMMIT, explicit, OTHER_COMMIT) == OTHER_COMMIT
        assert reconcile_cites(stored, None) == stored
        assert reconcile_cites(None, text) == text

    def test_the_jsonl_store_keeps_both_explicit_cites(self, tmp_path):
        repo = _repo(tmp_path)
        stored = self._two(repo)
        store = LessonStore(base_dir=tmp_path / "home")
        store.save(Lesson(ts="t", rule=RULE, category="tool", cites=stored, cited_commit=COMMIT))
        text, commit = self._text_only(repo, f"never edit {SOURCE}")
        outcome = store.save_or_enrich(
            Lesson(ts="t", rule=RULE, category="tool", cites=text, cited_commit=commit)
        )
        assert outcome == "unchanged"
        assert store.load_all()[0].cites == stored

    def test_the_vector_store_keeps_both_explicit_cites_and_does_not_re_hash(self, tmp_path):
        repo = _repo(tmp_path)
        stored = self._two(repo)
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(RULE, "tool", None, cites=stored, cited_commit=COMMIT)
            (repo / SOURCE).write_text("edited\n")
            text, commit = self._text_only(repo, f"never edit {SOURCE}")
            result = store.write_lesson(RULE, "tool", None, cites=text, cited_commit=commit)
            assert result.outcome is LessonWriteOutcome.UNCHANGED
            assert json.loads(store.get_lessons()[0]["value_json"])["cites"] == stored

    def test_the_vector_store_adds_a_newly_named_file_to_the_stored_set(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "src" / "c.py").write_text("c = 1\n")
        stored = _cite(repo)
        store = _vector_store(tmp_path)
        with closing(store):
            store.write_lesson(RULE, "tool", None, cites=stored, cited_commit=COMMIT)
            text, commit = self._text_only(repo, "see src/c.py")
            result = store.write_lesson(RULE, "tool", None, cites=text, cited_commit=OTHER_COMMIT)
            assert result.outcome is LessonWriteOutcome.ENRICHED
            value = json.loads(store.get_lessons()[0]["value_json"])
            assert [c["path"] for c in value["cites"]] == [SOURCE, "src/c.py"]
            assert value["cited_commit"] == COMMIT


class TestALinkIsAuthorizedAtItsTargetAndGitMetadataIsAuthorizedToo:
    @requires_symlinks
    def test_a_link_to_a_target_the_session_may_not_read_is_refused_at_write(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "secrets").mkdir()
        (repo / "secrets" / "plan.txt").write_text("private\n")
        (repo / "src" / "alias.txt").symlink_to(repo / "secrets" / "plan.txt")

        def allow_only_the_name(path: Path) -> bool:
            return "secrets" not in path.parts

        captured = capture_cites(["src/alias.txt"], [], repo, may_read=allow_only_the_name)
        assert captured.cites is None
        assert captured.refused[0][1] == "does not resolve to a file inside the project"

    @requires_symlinks
    def test_a_link_to_a_target_the_session_may_not_read_is_left_unchecked_on_a_recheck(
        self, tmp_path
    ):
        repo = _repo(tmp_path)
        (repo / "secrets").mkdir()
        (repo / "secrets" / "plan.txt").write_text("private\n")
        (repo / "src" / "alias.txt").symlink_to(repo / "secrets" / "plan.txt")
        cites = [{"path": "src/alias.txt", "sha256": "c" * 64}]

        with patch("kiro_crew.lesson_cites._file_digest") as digest:
            review = CiteReview(repo, may_read=lambda path: "secrets" not in path.parts)
            assert review.annotate(cites, scope_satisfied=True) == (True, "")
        digest.assert_not_called()

    def test_head_metadata_is_authorized_before_it_is_read(self, tmp_path):
        repo = _repo(tmp_path)
        asked: list[str] = []

        def deny_head(path: Path) -> bool:
            asked.append(path.name)
            return path.name != "HEAD"

        assert read_head_commit(repo, deny_head) is None
        assert "HEAD" in asked
        assert read_head_commit(repo, lambda path: True) == COMMIT

    def test_a_capture_reads_no_git_metadata_the_session_may_not(self, tmp_path):
        repo = _repo(tmp_path)
        captured = capture_cites([SOURCE], [], repo, may_read=lambda path: ".git" not in path.parts)
        assert captured.cites and captured.cited_commit is None

    def test_object_store_files_are_authorized_before_the_commit_lookup_reads_them(self, tmp_path):
        repo = _repo(tmp_path, with_commit=False)
        _pack_index(repo, [COMMIT])
        asked: list[str] = []

        def record(path: Path) -> bool:
            asked.append(path.name)
            return False

        assert commit_in_repo(repo, COMMIT, record) is False
        assert "pack-a.idx" in asked
        assert commit_in_repo(repo, COMMIT, lambda path: True) is True

    def test_each_metadata_file_is_asked_once_per_lookup(self, tmp_path):
        repo = _repo(tmp_path, with_commit=False)
        _pack_index(repo, ["c" * 40])
        asked: list[str] = []

        def record(path: Path) -> bool:
            asked.append(str(path))
            return True

        commit_in_repo(repo, OTHER_COMMIT, record)
        assert len(asked) == len(set(asked))


class TestATruncatedRecallBlockKeepsTheWithheldLine:
    def test_the_line_is_reported_whole_and_the_lesson_text_is_cut(self):
        from kiro_crew.lesson_cites import withheld_notice
        from kiro_crew.vector_memory_runtime.lessons import truncate_explicit_lessons

        notice = withheld_notice(1)
        block = (
            "[Learned corrections — user-taught rules from past mistakes.\n"
            "ALWAYS follow these. They override default behavior.]\n"
            f"- {'a long rule ' * 40}\n[End of learned corrections]\n{notice}"
        )
        cut = truncate_explicit_lessons(block, len(notice) + 260)
        assert cut.endswith(notice) and "… [truncated]" in cut
        assert len(cut) <= len(notice) + 260

    def test_a_block_that_already_fits_is_returned_as_is(self):
        from kiro_crew.vector_memory_runtime.lessons import truncate_explicit_lessons

        assert truncate_explicit_lessons("short", 100) == "short"


class TestAGitLocationReachedThroughALinkIsNotRead:
    """On Windows a junction can name a UNC share, and the first probe through it opens an
    outbound SMB connection, so a linked ``.git`` location is refused before any probe."""

    @requires_symlinks
    def test_a_linked_git_directory_gives_no_commit_and_is_never_asked_about(self, tmp_path):
        repo = _repo(tmp_path)
        real = tmp_path / "elsewhere.git"
        (repo / ".git").rename(real)
        (repo / ".git").symlink_to(real)
        asked: list[Path] = []

        def record(path: Path) -> bool:
            asked.append(path)
            return True

        assert read_head_commit(repo, record) is None
        assert commit_in_repo(repo, COMMIT, record) is False
        assert asked == []

    @requires_symlinks
    def test_a_linked_pack_directory_is_not_listed(self, tmp_path):
        repo = _repo(tmp_path, with_commit=False)
        real = tmp_path / "packs"
        real.mkdir()
        (repo / ".git" / "objects").mkdir(parents=True, exist_ok=True)
        (repo / ".git" / "objects" / "pack").symlink_to(real)
        with patch.object(Path, "iterdir", side_effect=AssertionError("listed a linked dir")):
            assert commit_in_repo(repo, OTHER_COMMIT) is False

    def test_a_junction_is_refused_before_it_is_probed(self, tmp_path):
        # A Windows junction is not a symlink to os.path.islink; the module asks the
        # platform helper, which a test can answer for any name.
        repo = _repo(tmp_path, with_commit=False)
        (repo / ".git" / "objects" / "pack").mkdir(parents=True)

        def junction(path) -> bool:
            return Path(path).name == "pack"

        with (
            patch("kiro_crew.lesson_cites.platform_compat.is_link_or_junction", junction),
            patch.object(Path, "iterdir", side_effect=AssertionError("listed a junction")),
        ):
            assert commit_in_repo(repo, OTHER_COMMIT) is False

    def test_a_junctioned_dot_git_is_refused_before_it_is_probed(self, tmp_path):
        repo = _repo(tmp_path)

        def junction(path) -> bool:
            return Path(path).name == ".git"

        with patch("kiro_crew.lesson_cites.platform_compat.is_link_or_junction", junction):
            assert read_head_commit(repo) is None

    @requires_symlinks
    def test_a_worktree_pointer_through_a_linked_directory_is_refused(self, tmp_path):
        main = _repo(tmp_path, "main")
        admin = main / ".git" / "worktrees" / "wt"
        admin.mkdir(parents=True)
        (admin / "HEAD").write_text(OTHER_COMMIT + "\n")
        hop = tmp_path / "hop"
        hop.symlink_to(main)
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {hop}/.git/worktrees/wt\n")
        assert read_head_commit(wt) is None

    def test_an_ordinary_worktree_pointer_still_works(self, tmp_path):
        main = _repo(tmp_path, "main")
        admin = main / ".git" / "worktrees" / "wt"
        admin.mkdir(parents=True)
        (admin / "HEAD").write_text(OTHER_COMMIT + "\n")
        (admin / "commondir").write_text("../..\n")
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text(f"gitdir: {admin}\n")
        assert read_head_commit(wt) == OTHER_COMMIT


class TestTheBoundsHoldAtTheInput:
    def test_only_the_first_cites_up_to_the_bound_are_looked_at(self, tmp_path):
        repo = _repo(tmp_path)
        looked_at: list[str] = []

        def spy(path: Path) -> bool:
            looked_at.append(path.name)
            return True

        names = [f"src/n{i}.py" for i in range(1000)]
        captured = capture_cites(names, [], repo, may_read=spy)
        assert len(looked_at) <= CITE_MAX
        assert captured.cites is None
        assert len(captured.refused) <= CITE_MAX + 1
        assert captured.refused[-1][0] == "(further cites)"

    def test_an_endless_iterable_is_not_consumed_past_the_bound(self, tmp_path):
        from itertools import count

        repo = _repo(tmp_path)
        captured = capture_cites((f"src/e{i}.py" for i in count()), [], repo)
        assert len(captured.refused) == CITE_MAX + 1

    def test_the_overflow_is_refused_before_a_file_is_read(self, tmp_path):
        repo = _repo(tmp_path)
        names = []
        for i in range(CITE_MAX):
            (repo / "src" / f"f{i}.py").write_text(str(i))
            names.append(f"src/f{i}.py")
        (repo / "src" / "extra.py").write_text("x")
        with patch("kiro_crew.lesson_cites._file_digest", wraps=lesson_cites_digest()) as digest:
            captured = capture_cites([*names, "src/extra.py"], [], repo)
        assert digest.call_count == CITE_MAX
        assert len(captured.cites or []) == CITE_MAX

    def test_only_a_bounded_number_of_pack_indexes_are_collected(self, tmp_path, monkeypatch):
        from kiro_crew import lesson_cites

        monkeypatch.setattr(lesson_cites, "_MAX_PACK_INDEXES", 3)
        repo = _repo(tmp_path, with_commit=False)
        pack = repo / ".git" / "objects" / "pack"
        pack.mkdir(parents=True)
        for i in range(50):
            (pack / f"pack-{i:02d}.idx").write_bytes(b"")
        opened: list[str] = []

        def spy(idx, wanted, may_read=None):
            opened.append(idx.name)
            return False

        monkeypatch.setattr(lesson_cites, "_pack_index_has", spy)
        assert commit_in_repo(repo, OTHER_COMMIT) is False
        assert len(opened) == 3


def lesson_cites_digest():
    from kiro_crew import lesson_cites

    return lesson_cites._file_digest


class TestAGitPointerNeverNamesAHost:
    @pytest.mark.parametrize(
        "pointer",
        ["\\\\evil\\share\\x", "//evil/share/x", "\\\\?\\UNC\\evil\\share", "\\\\.\\pipe\\x", "  "],
    )
    def test_a_unc_or_device_pointer_is_refused_before_anything_is_resolved(
        self, tmp_path, pointer
    ):
        from kiro_crew.lesson_cites import _git_dirs

        repo = tmp_path / "wt"
        repo.mkdir()
        (repo / ".git").write_text(f"gitdir: {pointer}\n")
        real_resolve, real_is_dir = Path.resolve, Path.is_dir

        def host_path(path: Path) -> bool:
            return str(path).startswith(("\\", "//"))

        def resolve(self, *args, **kwargs):
            assert not host_path(self), "resolved a host path"
            return real_resolve(self, *args, **kwargs)

        def is_dir(self, *args, **kwargs):
            assert not host_path(self), "probed a host path"
            return real_is_dir(self, *args, **kwargs)

        with patch.object(Path, "resolve", resolve), patch.object(Path, "is_dir", is_dir):
            assert _git_dirs(repo) is None

    def test_a_commondir_pointer_to_a_host_is_refused(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / ".git" / "commondir").write_text("\\\\evil\\share\n")
        assert read_head_commit(repo) == COMMIT
        assert commit_in_repo(repo, OTHER_COMMIT) is False

    def test_a_plain_relative_pointer_still_works(self, tmp_path):
        main = _repo(tmp_path, "main")
        admin = main / ".git" / "worktrees" / "wt"
        admin.mkdir(parents=True)
        (admin / "HEAD").write_text(OTHER_COMMIT + "\n")
        (admin / "commondir").write_text("../..\n")
        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt\n")
        assert read_head_commit(wt) == OTHER_COMMIT

    def test_only_a_bounded_pack_listing_is_read(self, tmp_path, monkeypatch):
        from kiro_crew import lesson_cites

        monkeypatch.setattr(lesson_cites, "_MAX_PACK_ENTRIES", 5)
        repo = _repo(tmp_path, with_commit=False)
        pack = repo / ".git" / "objects" / "pack"
        pack.mkdir(parents=True)
        for i in range(50):
            (pack / f"junk-{i:02d}.tmp").write_bytes(b"")
        opened: list[str] = []

        def spy(idx, wanted, may_read=None):
            opened.append(idx.name)
            return False

        monkeypatch.setattr(lesson_cites, "_pack_index_has", spy)
        assert commit_in_repo(repo, OTHER_COMMIT) is False
        assert opened == []


class TestTheAgentRunningTheSessionReachesGovernance:
    def test_vet_and_audit_passes_the_agent_to_the_resolver(self):
        from kiro_crew.platform import governance_profiles

        seen: dict[str, object] = {}

        def verdict(scope, item, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(permitted=True, rule="", layer="", reason="")

        with (
            patch.object(governance_profiles, "governance_permits", verdict),
            patch("kiro_crew.sel.sel"),
        ):
            governance_profiles.vet_and_audit(
                "filesystem.read", "/x", session_key="s", tool_name="t", agent="researcher"
            )
        assert seen["agent"] == "researcher"

    def test_a_recheck_asks_for_the_agent_the_builder_named(self, tmp_path):
        from kiro_crew.lesson_cites import cite_session
        from kiro_crew.platform import governance_profiles

        repo = _repo(tmp_path)
        store = _vector_store(tmp_path)
        store.write_lesson(RULE, "tool", None, cites=_cite(repo), cited_commit=COMMIT)
        (repo / SOURCE).write_text("changed\n")
        asked: list[tuple[str, str]] = []

        def verdict(scope, item, **kwargs):
            asked.append((kwargs.get("session_key", ""), kwargs.get("agent", "?")))
            return SimpleNamespace(permitted=True, rule="", layer="", reason="")

        with (
            closing(store),
            patch.object(governance_profiles, "governance_permits", verdict),
            patch("kiro_crew.sel.sel"),
            cite_session("s-1", "researcher"),
        ):
            store.get_lessons_context(project_dir=repo)
        assert asked and set(asked) == {("s-1", "researcher")}

    def test_a_nested_render_naming_only_the_session_keeps_the_agent(self):
        from kiro_crew.lesson_cites import _CITE_AGENT, _CITE_SESSION, cite_session

        with cite_session("s-1", "researcher"):
            with cite_session("s-1"):
                assert (_CITE_SESSION.get(), _CITE_AGENT.get()) == ("s-1", "researcher")
        assert (_CITE_SESSION.get(), _CITE_AGENT.get()) == ("test-session", "")

    def test_a_worker_names_the_agent_too(self):
        from concurrent.futures import ThreadPoolExecutor

        from kiro_crew.lesson_cites import _CITE_AGENT, run_for_session

        with ThreadPoolExecutor(max_workers=1) as pool:
            seen = pool.submit(
                run_for_session, "s-2", lambda: _CITE_AGENT.get(), _cite_agent="planner"
            ).result()
        assert seen == "planner"

    def test_the_route_asks_for_the_slots_agent(self):
        from kiro_crew.dashboard.handlers.cron import _session_agent

        state = SimpleNamespace(_slots={"ui": SimpleNamespace(agent="researcher")})
        assert _session_agent(state, "dashboard:ui") == "researcher"
        assert _session_agent(state, "dashboard:gone") == ""
        assert _session_agent(SimpleNamespace(_slots={"ui": SimpleNamespace(agent=3)}), "ui") == ""


class TestRepositoryDiscoveryNeverFollowsAGitLink:
    def test_the_walk_asks_lstat_and_never_follows(self, tmp_path):
        from kiro_crew.project_scope import find_repo_root

        repo = _repo(tmp_path)
        with patch.object(Path, "exists", side_effect=AssertionError("followed .git")):
            assert find_repo_root(repo) == repo.resolve()

    @requires_symlinks
    def test_a_linked_git_still_marks_the_boundary_without_being_followed(self, tmp_path):
        from kiro_crew.project_scope import _has_git_entry

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").symlink_to(tmp_path / "does-not-exist")
        with patch.object(Path, "exists", side_effect=AssertionError("followed .git")):
            assert _has_git_entry(repo) is True

    def test_a_missing_git_entry_is_absent(self, tmp_path):
        from kiro_crew.project_scope import _has_git_entry

        assert _has_git_entry(tmp_path) is False


class TestAMetadataFileReachedThroughALinkIsNotRead:
    """Authorization is asked about the name a file is reached by and the guarded reader
    opens what it resolves to, so a link under ``.git`` must not be followed."""

    @requires_symlinks
    def test_a_head_that_is_a_link_gives_no_commit_and_is_never_read(self, tmp_path):
        repo = _repo(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text(OTHER_COMMIT + "\n")
        (repo / ".git" / "HEAD").unlink()
        (repo / ".git" / "HEAD").symlink_to(outside)
        asked: list[str] = []

        def record(path: Path) -> bool:
            asked.append(path.name)
            return True

        assert read_head_commit(repo, record) is None
        assert "HEAD" not in asked

    @requires_symlinks
    def test_a_ref_that_is_a_link_is_not_followed(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (repo / ".git" / "refs" / "heads").mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text(OTHER_COMMIT + "\n")
        (repo / ".git" / "refs" / "heads" / "main").symlink_to(outside)
        assert read_head_commit(repo) is None

    @requires_symlinks
    def test_a_ref_directory_that_is_a_link_is_not_followed(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        real = tmp_path / "heads"
        real.mkdir()
        (real / "main").write_text(OTHER_COMMIT + "\n")
        (repo / ".git" / "refs").mkdir()
        (repo / ".git" / "refs" / "heads").symlink_to(real)
        assert read_head_commit(repo) is None

    @requires_symlinks
    def test_a_pack_index_that_is_a_link_is_not_read(self, tmp_path):
        repo = _repo(tmp_path, with_commit=False)
        _pack_index(repo, [COMMIT], name="real.idx")
        pack = repo / ".git" / "objects" / "pack"
        (pack / "pack-link.idx").symlink_to(pack / "real.idx")
        (pack / "real.idx").rename(tmp_path / "moved.idx")
        (pack / "pack-link.idx").unlink()
        (pack / "pack-link.idx").symlink_to(tmp_path / "moved.idx")
        assert commit_in_repo(repo, COMMIT) is False

    def test_ordinary_unlinked_metadata_is_still_read(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (repo / ".git" / "refs" / "heads").mkdir(parents=True)
        (repo / ".git" / "refs" / "heads" / "main").write_text(OTHER_COMMIT + "\n")
        assert read_head_commit(repo) == OTHER_COMMIT


class TestACiteIsNeverReachedThroughALink:
    @requires_symlinks
    def test_a_cite_through_a_linked_directory_is_refused_before_it_is_resolved(self, tmp_path):
        repo = _repo(tmp_path)
        real = repo / "real"
        real.mkdir()
        (real / "mod.py").write_text("x = 1\n")
        (repo / "alias").symlink_to(real)
        with patch("kiro_crew.lesson_cites.resolve_in_project") as probe:
            captured = capture_cites(["alias/mod.py"], [], repo)
        probe.assert_not_called()
        assert captured.cites is None and len(captured.refused) == 1

    @requires_symlinks
    def test_a_recheck_through_a_linked_directory_is_left_unchecked(self, tmp_path):
        repo = _repo(tmp_path)
        real = repo / "real"
        real.mkdir()
        (real / "mod.py").write_text("x = 1\n")
        (repo / "alias").symlink_to(real)
        row = [{"path": "alias/mod.py", "sha256": "c" * 64}]
        with patch("kiro_crew.lesson_cites.resolve_in_project") as probe:
            review = CiteReview(repo, may_read=lambda path: True)
            assert review.annotate(row, scope_satisfied=True) == (True, "")
        probe.assert_not_called()

    def test_a_junction_named_in_a_cite_is_refused_through_the_platform_helper(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "src" / "pkg").mkdir(exist_ok=True)

        def junction(path) -> bool:
            return Path(path).name == "pkg"

        with (
            patch("kiro_crew.lesson_cites.platform_compat.is_link_or_junction", junction),
            patch("kiro_crew.lesson_cites.resolve_in_project") as probe,
        ):
            captured = capture_cites([SOURCE], [], repo)
        probe.assert_not_called()
        assert captured.cites is None

    @requires_symlinks
    def test_a_loose_object_that_is_a_link_is_not_followed(self, tmp_path):
        repo = _repo(tmp_path, with_commit=False)
        target = tmp_path / "blob"
        target.write_bytes(b"x")
        folder = repo / ".git" / "objects" / OTHER_COMMIT[:2]
        folder.mkdir(parents=True)
        (folder / OTHER_COMMIT[2:]).symlink_to(target)
        assert commit_in_repo(repo, OTHER_COMMIT) is False
