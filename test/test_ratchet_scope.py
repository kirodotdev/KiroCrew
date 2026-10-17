"""The shared scope resolver must tell the two merge shapes apart.

``scripts/ratchet_scope.py`` answers "which files did THIS change touch" for the
merge-ref ratchets. Two checkout shapes both look like "HEAD is a merge" and
need opposite diffs:

* CI's ``pull_request`` merge ref: the BASE is the first parent, so
  ``HEAD^1..HEAD`` is exactly the PR's own change.
* A local ``git merge origin/main`` on a feature branch: the FEATURE tip is the
  first parent, so ``HEAD^1..HEAD`` is only what main brought in and the
  branch's own commits are invisible -- every consuming gate then under-scopes,
  and a violation added in an earlier feature commit passes locally only to red
  the PR on CI.

The resolver decides by asking git which parent the base branch can reach, so
these tests build one synthetic repo per shape and pin the attempt LABEL chosen
plus the returned path set. The three-dot fallback deliberately keeps diffing
from ``merge-base(base, HEAD)`` rather than the base tip: an unscoped gate has
already been observed reporting files the base branch merged after the baseline
was taken, and the CI-shape test locks that property by moving main after the
branch point.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS, git_command_env

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ratchet_scope.py"

SPEC = importlib.util.spec_from_file_location("ratchet_scope", SCRIPT)
assert SPEC and SPEC.loader
scope = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(scope)


#: Config a ``git -c ...`` that ran the suite (a pre-push hook) exports to its children.
#: git reads it after ``GIT_CONFIG_COUNT``, so it would override any pin set there.
_GIT_COMMAND_LINE_CONFIG = ("GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT")


def _fixture_git_env() -> dict[str, str]:
    """Env for a fixture git call: no inherited location, templates, hooks, or identity.

    ``git_command_env()`` (the production chokepoint) strips the ``GIT_DIR``
    location family -- those must be ABSENT, and a merge over ``os.environ``
    can only add keys -- and pins the fixed-key exec vectors. On top of that,
    an inherited ``GIT_TEMPLATE_DIR`` (or a global ``init.templateDir``) would
    have its hooks COPIED into every fixture repo by ``git init`` and executed
    by the ``git commit`` below -- host-side effects from running the test
    suite -- so both template channels are pinned empty. Identity is supplied
    so a commit cannot depend on, or fall back to, the developer's global
    config, which is itself pointed at ``os.devnull``.
    """
    env = {
        **git_command_env(),
        "GIT_TEMPLATE_DIR": "",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
    }
    env.pop("GIT_CONFIG_PARAMETERS", None)  # see _GIT_COMMAND_LINE_CONFIG
    count = int(env["GIT_CONFIG_COUNT"])
    env[f"GIT_CONFIG_KEY_{count}"] = "init.templateDir"
    env[f"GIT_CONFIG_VALUE_{count}"] = ""
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    return env


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=_fixture_git_env(),
    )
    return proc.stdout.strip()


def _commit_file(repo: Path, name: str, message: str) -> None:
    (repo / name).write_text(f"{name}\n", encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", message)


def _build_repo_with_diverged_feature(repo: Path) -> None:
    """Populate ``repo`` with the one base shape every test in this module starts
    from.

    ``main`` gains ``mainline.txt`` AFTER ``feature`` branches off with its own
    ``feature.py``, so the two sides of every merge below differ and a wrong
    parent choice shows up in the returned path set, not just the label.
    """
    _git(repo, "init", "-b", "main", ".")
    _commit_file(repo, "base.txt", "base")
    _git(repo, "checkout", "-b", "feature")
    _commit_file(repo, "feature.py", "the change under judgment")
    _git(repo, "checkout", "main")
    _commit_file(repo, "mainline.txt", "someone else's change, landed after the branch point")


@pytest.fixture(scope="session")
def _repo_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the diverged-feature repo once per session; ``repo`` copies it per test.

    Six git subprocesses (~2-3s) would otherwise be paid on every one of the 23
    tests in this module. Session scope is safe here because the template is
    never handed to a test, only copied from via ``shutil.copytree`` -- so a
    test that adds a commit, merges, or moves a branch cannot reach another's
    copy.
    """
    template = tmp_path_factory.mktemp("ratchet-scope-seed") / "repo"
    template.mkdir()
    _build_repo_with_diverged_feature(template)
    return template


def _set_origin_main(repo: Path) -> None:
    # The synthetic repo has no remote; the resolver only needs the REF, so
    # point origin/main at the local main tip directly.
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _repo_template: Path) -> Path:
    # The fixture's own git calls build a scrubbed env per call, but the
    # RESOLVER under test runs git with the ambient process environment: an
    # exported GIT_DIR (pytest run from a git hook, `git rebase --exec`,
    # `git bisect run`) would override its cwd=ROOT and answer for the wrong
    # repository. Delete the whole location family -- monkeypatch restores it
    # after the test -- using the same canonical list the production env
    # builder strips.
    for var in (*_GIT_LOCATION_VARS, *_GIT_COMMAND_LINE_CONFIG):
        monkeypatch.delenv(var, raising=False)
    fixture_repo = tmp_path / "repo"
    shutil.copytree(_repo_template, fixture_repo)
    # A copied checkout reads as "unstaged changes" on Windows (fresh inode/ctime
    # invalidate the index stat cache); nothing in the template is uncommitted, so
    # this changes no content and only re-stats the index.
    _git(fixture_repo, "reset", "--hard", "HEAD")
    # The module runs git with cwd=ROOT; retarget it at the synthetic repo.
    monkeypatch.setattr(scope, "ROOT", fixture_repo)
    return fixture_repo


class TestMergeShapes:
    def test_ci_merge_ref_scopes_to_the_change_only(self, repo: Path) -> None:
        # GitHub's pull_request merge ref: merge the PR INTO the base, so the
        # base tip is the first parent and origin/main can reach it.
        _set_origin_main(repo)
        _git(repo, "checkout", "--detach", "main")
        _git(repo, "merge", "--no-ff", "-m", "merge ref", "feature")

        paths, label = scope.changed_paths()

        assert label == "merge HEAD^1..HEAD"
        # Exactly the PR's own change: mainline.txt landed on the base after
        # the branch point and must NOT be judged as part of this change.
        assert paths == {"feature.py"}

    def test_local_merge_of_main_scopes_to_the_branch_own_commits(self, repo: Path) -> None:
        # The inverted shape: `git merge origin/main` ON the feature branch
        # puts the feature tip first. HEAD^1..HEAD here is what main brought
        # in, so taking the merge diff would hide feature.py -- the defect this
        # resolver exists to avoid. The base-reachability probe must reject the
        # merge attempts and fall through to the three-dot diff.
        _set_origin_main(repo)
        _git(repo, "checkout", "feature")
        _git(repo, "merge", "--no-ff", "-m", "sync with main", "origin/main")

        paths, label = scope.changed_paths()

        assert label == "origin/main...HEAD"
        assert paths == {"feature.py"}

    def test_merge_made_on_main_is_still_recognised_without_a_remote(self, repo: Path) -> None:
        # A merge made ON main itself (no remote at all): the prior main tip is
        # the first parent, and the local `main` ref -- now the merge commit --
        # reaches it. The probe must accept this via the `main` fallback;
        # probing only origin/main would reject it, and `main...HEAD` then
        # diffs the merge against itself: an EMPTY scope, so every consuming
        # gate passes vacuously -- a false green in the same direction as the
        # under-scope this resolver exists to prevent.
        _git(repo, "merge", "--no-ff", "-m", "land feature", "feature")

        paths, label = scope.changed_paths()

        assert label == "merge HEAD^1..HEAD"
        assert paths == {"feature.py"}

    def test_unverifiable_base_falls_through_rather_than_trusting_parent_order(
        self, repo: Path
    ) -> None:
        # No origin/main at all: the reachability probe cannot verify either
        # way, and an unverified merge diff is the failure mode above. Falling
        # through is the safe direction -- here the local `main` ref still
        # answers the three-dot question correctly.
        _git(repo, "checkout", "feature")
        _git(repo, "merge", "--no-ff", "-m", "sync with main", "main")

        paths, label = scope.changed_paths()

        assert label == "main...HEAD"
        assert paths == {"feature.py"}


class TestWholeTreeOverride:
    """A push to the base branch has no diff, and that is not a clean tree.

    The Main Ratchet Audit lane runs on a push to ``main``, where the checkout
    leaves HEAD, ``main`` and ``origin/main`` all at the pushed commit. The
    three-dot fallback then succeeds with an EMPTY path set -- exit 0, no
    attempt fails, nothing marks the answer as unusable -- and every consuming
    ratchet filters its violations against that empty set and reports green
    whatever the tree holds. The override is how a caller states that the
    question really is about a whole tree, instead of inheriting an answer that
    depends on which checkout shape a run happened to get.
    """

    def test_a_push_to_the_base_branch_resolves_to_an_empty_diff(self, repo: Path) -> None:
        # The hazard itself, pinned: HEAD *is* origin/main, so the scope is
        # empty rather than undeterminable, and a gate cannot tell the
        # difference between this and a change that touched nothing.
        _set_origin_main(repo)

        paths, label = scope.changed_paths()

        assert label == "origin/main...HEAD"
        assert paths == set()

    def test_the_override_answers_whole_tree(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_origin_main(repo)
        monkeypatch.setenv(scope.WHOLE_TREE_ENV, "1")

        paths, label = scope.changed_paths()

        assert paths is None
        assert scope.WHOLE_TREE_ENV in label

    def test_the_override_wins_over_a_resolvable_diff(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `feature` has a real diff against main, so without the override this
        # shape scopes to one file. The answer must not depend on that: a caller
        # asking for the whole tree gets it in every checkout shape.
        _git(repo, "checkout", "feature")
        _set_origin_main(repo)
        monkeypatch.setenv(scope.WHOLE_TREE_ENV, "1")

        paths, _ = scope.changed_paths()

        assert paths is None

    def test_a_blank_value_is_not_an_opt_in(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Actions writes an empty string for an unset expression, so a blank
        # value must read as absent rather than silently widening every gate.
        _set_origin_main(repo)
        monkeypatch.setenv(scope.WHOLE_TREE_ENV, "  ")

        paths, label = scope.changed_paths()

        assert paths == set()
        assert label == "origin/main...HEAD"

    def test_the_whole_tree_label_is_not_read_as_a_diff_range(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `added_lines` dispatches on the label, so an unrecognised one must
        # degrade to None (the added-line rule is skipped) rather than being
        # handed to `git diff` as a revision. The label comes from the resolver
        # itself: a hardcoded copy would keep passing after the production
        # label grew a `...`-shaped suffix that IS read as a revision.
        _set_origin_main(repo)
        monkeypatch.setenv(scope.WHOLE_TREE_ENV, "1")
        _, label = scope.changed_paths()

        assert scope.added_lines(label) is None

    def test_base_ref_names_the_commit_each_label_diffs_against(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # check_black_formatting reads the baseline at this commit; a second
        # copy of the label mapping there is how the two would come to disagree.
        _set_origin_main(repo)
        merge_base = _git(repo, "merge-base", "origin/main", "HEAD")
        assert scope.base_ref("origin/main...HEAD") == merge_base.strip()
        assert scope.base_ref("merge HEAD^1..HEAD") == "HEAD^1"
        assert scope.base_ref("merge parents") == "HEAD^1"
        assert scope.base_ref("nosuchref...HEAD") is None
        monkeypatch.setenv(scope.WHOLE_TREE_ENV, "1")
        _, label = scope.changed_paths()
        assert scope.base_ref(label) is None

    def test_the_audit_lane_opts_in(self) -> None:
        # The wiring, not the mechanism: without this env the Main Ratchet Audit
        # runs four gates over zero files and reports a green verdict on main
        # that means nothing -- which is the exact false all-clear the lane
        # exists to prevent.
        workflow = ROOT / ".github" / "workflows" / "main-ratchet-audit.yml"
        body = workflow.read_text(encoding="utf-8")

        assert f"{scope.WHOLE_TREE_ENV}:" in body, (
            f"{workflow.name} no longer sets {scope.WHOLE_TREE_ENV}, so its black / "
            "subprocess-encoding / agent-SDK / sync-IO gates scope to the push's "
            "own diff -- which on main is empty, making all four pass by measuring "
            "nothing"
        )


class TestDirtyTree:
    """``added_lines`` must describe the file state the consuming gates scan.

    The merge-ref ratchets read violations from the WORKING TREE. On a tree
    with uncommitted edits, an added set numbered by HEAD's copy of a file and
    a violation numbered by the working-tree copy can collide: a pre-existing
    line shifted by an uncommitted insert lands on a line number the HEAD diff
    counts as added, and the gate fails on bytes that pass once committed.
    These tests pin the three-dot label's diff to base-to-working-tree, and pin
    that the base stays the MERGE-BASE rather than the base tip.
    """

    def test_uncommitted_edits_are_in_the_added_set(self, repo: Path) -> None:
        _git(repo, "checkout", "feature")
        _set_origin_main(repo)
        (repo / "feature.py").write_text("feature.py\nuncommitted\n", encoding="utf-8")

        added = scope.added_lines("origin/main...HEAD")

        assert added is not None
        assert 2 in added["feature.py"]

    def test_a_shifted_preexisting_line_is_not_counted_as_added(self, repo: Path) -> None:
        # The reproduction from the comment-history gate: `victim.py` exists on
        # the base with a line the baseline already records, the branch appends
        # committed lines below it, and an uncommitted insert ABOVE it shifts
        # the recorded line onto a number the base..HEAD diff counts as added.
        # The added set must describe the working tree, where that line is old.
        (repo / "victim.py").write_text("recorded = 1\n", encoding="utf-8")
        _git(repo, "add", "victim.py")
        _git(repo, "commit", "-m", "base file with a recorded line")
        _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        _git(repo, "checkout", "-b", "topic")
        (repo / "victim.py").write_text(
            "recorded = 1\nadded_a = 2\nadded_b = 3\n", encoding="utf-8"
        )
        _git(repo, "add", "victim.py")
        _git(repo, "commit", "-m", "append two lines below the recorded one")
        # Uncommitted: insert two lines above. The recorded line now sits at
        # working-tree line 3, a number inside HEAD's added range {2, 3}.
        (repo / "victim.py").write_text(
            "wip_a = 0\nwip_b = 0\nrecorded = 1\nadded_a = 2\nadded_b = 3\n", encoding="utf-8"
        )

        added = scope.added_lines("origin/main...HEAD")

        assert added is not None
        assert 3 not in added["victim.py"]
        assert {1, 2}.issubset(added["victim.py"])

    def test_the_base_is_the_merge_base_not_the_base_tip(self, repo: Path) -> None:
        # main rewrites base.txt AFTER the branch point. A diff against the
        # base TIP would count the working tree's older copy of base.txt as
        # this change's added lines; the merge-base diff sees no edit there.
        _git(repo, "checkout", "main")
        (repo / "base.txt").write_text("rewritten on main\n", encoding="utf-8")
        _git(repo, "add", "base.txt")
        _git(repo, "commit", "-m", "rewrite base.txt on main after the branch point")
        _set_origin_main(repo)
        _git(repo, "checkout", "feature")
        (repo / "feature.py").write_text("feature.py\nuncommitted\n", encoding="utf-8")

        added = scope.added_lines("origin/main...HEAD")

        assert added is not None
        assert "base.txt" not in added
        assert 2 in added["feature.py"]

    def test_a_clean_tree_matches_the_head_diff(self, repo: Path) -> None:
        # CI checks out a clean tree; the working-tree diff must be identical
        # to the committed one there, so this change is invisible to CI.
        _git(repo, "checkout", "feature")
        _set_origin_main(repo)

        added = scope.added_lines("origin/main...HEAD")

        assert added == {"feature.py": {1}}


class TestExplicitBase:
    """The env-base family's entry points: an EXPLICIT ref in, shared parsing out.

    ``check_brand_name.py``, ``check_harness_parity.py`` and
    ``check_focus_cue.py`` are handed their base through ``*_BASE_REF`` (CI
    resolves it to the PR's ``base.sha``), so unlike the resolver above they
    never discover the checkout shape — but the diff parsing must be the same
    code, or the same added line gets judged differently by different gates.
    """

    def test_the_entry_points_are_reachable_by_name(self) -> None:
        # The gates call these through a path-loaded module, so a rename there
        # must fail HERE, not as an AttributeError inside a CI run.
        assert callable(scope.resolve_base)
        assert callable(scope.changed_paths_at)
        assert callable(scope.added_lines_at)
        assert callable(scope.parse_added_lines)

    def test_changed_paths_at_diffs_from_the_named_commit_only(self, repo: Path) -> None:
        # The base.sha property: an explicit commit in, exactly the work after
        # it out — moving any branch ref afterwards must change nothing,
        # because a run started against base.sha must not pick up base moves
        # landing after it started.
        base = _git(repo, "rev-parse", "main~1")
        _git(repo, "branch", "-f", "release", "main")  # a ref move, post-capture

        assert scope.changed_paths_at(base) == ["mainline.txt"]

    def test_changed_paths_at_sees_the_working_tree(self, repo: Path) -> None:
        # Base-to-working-tree: a local run must see edits that are not
        # committed yet, which is the only form in which a local run is useful.
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "base.txt").write_text("edited, not committed\n", encoding="utf-8")

        assert scope.changed_paths_at(base) == ["base.txt"]

    def test_changed_paths_at_does_not_quote_a_non_ascii_path(self, repo: Path) -> None:
        # `-z` output is never quoted, so a path with non-ASCII bytes comes
        # back byte-exact — usable as a pathspec for the per-path diff. A
        # quoted `"b/\346..."` name is how a parser silently drops a file.
        base = _git(repo, "rev-parse", "HEAD")
        _commit_file(repo, "日本語.md", "non-ascii name")

        assert scope.changed_paths_at(base) == ["日本語.md"]
        assert scope.added_lines_at(base, "日本語.md") == {1}

    def test_changed_paths_at_fails_closed_on_an_unresolvable_base(self, repo: Path) -> None:
        # The env-base gates refuse to pass when they cannot see their base —
        # unlike changed_paths(), which degrades to whole-tree scope. The
        # raised error is the seam each gate wraps in its own fail-closed
        # message.
        with pytest.raises(subprocess.CalledProcessError):
            scope.changed_paths_at("no-such-ref")

    def test_added_lines_at_names_the_added_lines(self, repo: Path) -> None:
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "base.txt").write_text("base.txt\nnew two\nnew three\n", encoding="utf-8")

        assert scope.added_lines_at(base, "base.txt") == {2, 3}

    def test_a_pure_deletion_contributes_nothing_by_default(self, repo: Path) -> None:
        (repo / "three.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        _git(repo, "add", "three.txt")
        _git(repo, "commit", "-m", "three lines")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "three.txt").write_text("one\nthree\n", encoding="utf-8")

        assert scope.added_lines_at(base, "three.txt") == set()

    def test_anchor_deletions_marks_where_the_lines_were_removed(self, repo: Path) -> None:
        # The focus-cue gate's semantics: the edit that most often removes a
        # cue is a pure deletion, invisible to the added set, so the `+N,0`
        # hunk is anchored at N instead of dropped.
        (repo / "three.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        _git(repo, "add", "three.txt")
        _git(repo, "commit", "-m", "three lines")
        base = _git(repo, "rev-parse", "HEAD")
        (repo / "three.txt").write_text("one\nthree\n", encoding="utf-8")

        assert scope.added_lines_at(base, "three.txt", anchor_deletions=True) == {1}

    def test_resolve_base_prefers_the_merge_base(self, repo: Path) -> None:
        # main moved after feature branched off; measuring from the main TIP
        # would charge main's own commits to the feature diff. The honest
        # divergence point is the merge-base.
        _git(repo, "checkout", "feature")

        expected = _git(repo, "merge-base", "main", "HEAD")
        assert scope.resolve_base("main") == expected
        assert scope.resolve_base("main") != _git(repo, "rev-parse", "main")

    def test_resolve_base_falls_back_to_the_base_tip(self, repo: Path) -> None:
        # A shallow CI clone fetches the base as its own tip with no shared
        # history: merge-base fails, and the ref itself has to serve.
        _git(repo, "checkout", "--orphan", "detached")
        _git(repo, "commit", "-m", "unrelated root")

        assert scope.resolve_base("main") == "main"

    def test_the_fallback_announces_that_it_degraded(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A verdict computed against the tip must not look like one from the merge base.

        The fallback answers a question it cannot answer exactly, and the pair
        (exact answer, degraded answer) is indistinguishable in every gate's
        report. So the degradation is stated where the gates already print, as a
        ``warning`` so CI surfaces it as an annotation.
        """
        _git(repo, "checkout", "--orphan", "detached")
        _git(repo, "commit", "-m", "unrelated root")

        scope.resolve_base("main")

        out = capsys.readouterr().out
        assert "::warning::" in out
        assert "base TIP" in out

    def test_the_merge_base_path_announces_too(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Silence on success would make a lost announcement look like a clean run."""
        _git(repo, "checkout", "feature")

        resolved = scope.resolve_base("main")

        out = capsys.readouterr().out
        assert "::notice::" in out
        assert resolved in out
        assert "::warning::" not in out


class TestParseAddedLines:
    """The text-level parser, reachable without a repository."""

    DIFF = (
        "diff --git a/a.txt b/a.txt\n"
        "index 000..111 100644\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1,0 +2,3 @@ some context\n"
        "+two\n"
        "+three\n"
        "+four\n"
        "@@ -9,2 +11,0 @@\n"
        "-gone\n"
        "-gone\n"
        "@@ -20 +21 @@\n"
        "-old\n"
        "+new\n"
    )

    def test_added_hunks_with_and_without_counts(self) -> None:
        # `+2,3` names three lines; a bare `+21` means exactly one; the
        # deletion-only `+11,0` contributes nothing by default.
        assert scope.parse_added_lines(self.DIFF) == {2, 3, 4, 21}

    def test_anchor_deletions_adds_the_deletion_point(self) -> None:
        assert scope.parse_added_lines(self.DIFF, anchor_deletions=True) == {2, 3, 4, 11, 21}


def test_env_base_gates_delegate_to_the_shared_plumbing() -> None:
    """Every added-line gate reads its diff through ratchet_scope.

    A private hunk parser per gate is the divergence this module exists to
    close: the same added line judged differently by different gates, and a
    scope fix to one copy leaving the others wrong. The hunk-header regex is
    the private parser's signature, so its absence is the pin — the focus-cue
    self-test may still grep raw ``@@`` lines to validate its PROBE's input,
    which is not parsing.
    """
    for name in ("check_brand_name.py", "check_harness_parity.py", "check_focus_cue.py"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "ratchet_scope.py" in source, f"{name} no longer uses the shared plumbing"
        assert r"\+(\d+)(?:,(\d+))?" not in source, f"{name} grew a private hunk parser back"


# ── fork-aware helpers (change_merge_base / change_files / show_at) ────


def _drop_ambient_git_location(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helpers under test run git with the ambient env, so an exported ``GIT_DIR``
    or ``GIT_INDEX_FILE`` (pytest run from a git hook) would answer for the wrong
    repository, and an exported ``git -c`` config would change what they read. Delete
    both, as the ``repo`` fixture above does."""
    for var in (*_GIT_LOCATION_VARS, *_GIT_COMMAND_LINE_CONFIG):
        monkeypatch.delenv(var, raising=False)


def assert_fixture_ignores_ambient_git(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fixture: str,
) -> None:
    """Build *fixture* under an inherited ``GIT_INDEX_FILE`` and a ``GIT_TEMPLATE_DIR``
    whose post-commit hook writes a marker: the foreign index stays untouched and the
    hook never runs. A commit on the scrubbed env with only the template's hooks put
    back proves the hook would run on this host, so the absence means something."""
    template = tmp_path / "template"
    (template / "hooks").mkdir(parents=True)
    marker = tmp_path / "hook-ran"
    hook = template / "hooks" / "post-commit"
    # LF on every host: Git for Windows' sh would read a CRLF line's redirect target as
    # "hook-ran\r" and the marker would never appear under its real name.
    hook.write_text(
        f'#!/bin/sh\necho ran > "{marker.as_posix()}"\n', encoding="utf-8", newline="\n"
    )
    hook.chmod(0o755)
    index = tmp_path / "foreign-index"
    index.write_bytes(b"not an index")
    monkeypatch.setenv("GIT_TEMPLATE_DIR", str(template))
    monkeypatch.setenv("GIT_INDEX_FILE", str(index))
    hooks = (template / "hooks").as_posix()
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'core.hooksPath'='{hooks}'")
    scrubbed = _fixture_git_env()
    assert "GIT_INDEX_FILE" not in scrubbed, "the fixture env keeps a foreign index"
    assert "GIT_CONFIG_PARAMETERS" not in scrubbed, "the fixture env keeps git -c options"
    request.getfixturevalue(fixture)
    # The helpers under test read the ambient env: the fixture must have dropped both.
    ambient = sorted({*_GIT_LOCATION_VARS, *_GIT_COMMAND_LINE_CONFIG} & set(os.environ))
    assert not ambient, f"the fixture left {ambient} for the helpers under test"
    assert index.read_bytes() == b"not an index", "a fixture git call used the foreign index"
    assert not marker.exists(), "a fixture commit ran an inherited template hook"
    copied = [path for path in tmp_path.rglob("post-commit") if template not in path.parents]
    assert not copied, f"git init copied the inherited template's hook: {copied}"
    control = tmp_path / "control"
    control.mkdir()
    env = _fixture_git_env()
    env["GIT_TEMPLATE_DIR"] = str(template)
    count = int(env["GIT_CONFIG_COUNT"])  # undo the scrubbed env's core.hooksPath pin
    env[f"GIT_CONFIG_KEY_{count}"] = "core.hooksPath"
    env[f"GIT_CONFIG_VALUE_{count}"] = ".git/hooks"
    env["GIT_CONFIG_COUNT"] = str(count + 1)
    for argv in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "control"]):
        subprocess.run(["git", *argv], cwd=control, env=env, check=True, capture_output=True)
    assert marker.exists(), "the inherited hook never runs here, so this proves nothing"


class _RepoHelper:
    """A tiny ``Repo`` wrapper holding a ``git(*argv)`` runner so a fixture can keep both."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def git(self, *argv: str) -> str:
        # The scrubbed fixture env: no inherited location, template hooks or identity.
        return _git(self.root, *argv)

    def __truediv__(self, name: str) -> Path:
        return self.root / name


class TestChangeMergeBase:
    """The change-shaped base resolver, for a per-file comparison that must handle a
    forked ``origin`` and a shallow CI checkout.
    """

    @pytest.fixture
    def repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _RepoHelper:
        _drop_ambient_git_location(monkeypatch)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        root = tmp_path / "repo"
        root.mkdir()
        repo = _RepoHelper(root)
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        repo.git("init", "-q")
        repo.git("checkout", "-q", "-b", "main")
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "base")
        repo.git("checkout", "-q", "-b", "change")
        (root / "b.txt").write_text("two\n", encoding="utf-8")
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "add b.txt")
        return repo

    def test_the_fixture_ignores_ambient_git_state(self, request, monkeypatch, tmp_path) -> None:
        assert_fixture_ignores_ambient_git(request, monkeypatch, tmp_path, "repo")

    def test_the_local_base_branch_is_used_when_nothing_else_resolves(
        self, repo: _RepoHelper
    ) -> None:
        base, how = scope.change_merge_base(cwd=repo.root, env={})
        assert base == repo.git("rev-parse", "main") and "main" in how

    def test_a_fork_s_stale_origin_loses_to_a_newer_upstream(self, repo: _RepoHelper) -> None:
        # upstream/main picks up a commit origin/main has not (the fork lags).
        repo.git("checkout", "-q", "main")
        stale_main = repo.git("rev-parse", "main")
        (repo / "c.txt").write_text("three\n", encoding="utf-8")
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "merged upstream")
        new_main = repo.git("rev-parse", "main")
        # change rebases onto the new main so its merge-base with upstream/main is new_main.
        repo.git("checkout", "-q", "change")
        repo.git("rebase", "-q", "main")
        repo.git("update-ref", "refs/remotes/origin/main", stale_main)
        repo.git("update-ref", "refs/remotes/upstream/main", new_main)
        base, how = scope.change_merge_base(cwd=repo.root, env={})
        assert base == new_main and "upstream/main" in how

    def test_the_env_override_wins_and_an_unresolvable_value_is_rejected(
        self, repo: _RepoHelper
    ) -> None:
        change = repo.git("rev-parse", "change")
        base, how = scope.change_merge_base(cwd=repo.root, env={"FLAKE_RATCHET_BASE": "change"})
        assert base == change and "FLAKE_RATCHET_BASE" in how
        assert (
            scope.change_merge_base(cwd=repo.root, env={"FLAKE_RATCHET_BASE": "no-such-ref"})[0]
            is None
        )

    def test_a_pull_request_merge_commit_s_first_parent_is_the_base(
        self, repo: _RepoHelper
    ) -> None:
        before = repo.git("rev-parse", "main")
        repo.git("checkout", "-q", "main")
        repo.git("merge", "-q", "--no-ff", "-m", "merge", "change")
        base, how = scope.change_merge_base(cwd=repo.root, env={"GITHUB_BASE_REF": "main"})
        assert base == before and "first parent" in how

    def test_a_push_event_uses_the_first_parent_too(self, repo: _RepoHelper) -> None:
        before = repo.git("rev-parse", "HEAD")
        (repo / "a.txt").write_text("edited\n", encoding="utf-8")
        repo.git("commit", "-q", "-am", "another")
        base, how = scope.change_merge_base(cwd=repo.root, env={"GITHUB_EVENT_NAME": "push"})
        assert base == before and "push" in how

    def test_an_unreadable_merge_base_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        _drop_ambient_git_location(monkeypatch)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
        root = tmp_path / "bare"
        root.mkdir()
        assert scope.change_merge_base(cwd=root, env={})[0] is None


class TestChangeFilesAndShowAt:
    """``change_files`` reports (base path, now path) for each file the working tree
    changes against *base*, and ``show_at`` reads a file's bytes at that base."""

    @pytest.fixture
    def repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _RepoHelper:
        _drop_ambient_git_location(monkeypatch)
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
        root = tmp_path / "repo"
        root.mkdir()
        repo = _RepoHelper(root)
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        (root / "b.txt").write_text("two\n", encoding="utf-8")
        (root / "to_delete.txt").write_text("gone\n", encoding="utf-8")
        repo.git("init", "-q")
        repo.git("checkout", "-q", "-b", "main")
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "base")
        repo.git("checkout", "-q", "-b", "change")
        return repo

    def test_the_fixture_ignores_ambient_git_state(self, request, monkeypatch, tmp_path) -> None:
        assert_fixture_ignores_ambient_git(request, monkeypatch, tmp_path, "repo")

    def _base(self, repo: _RepoHelper) -> str:
        return repo.git("rev-parse", "main")

    def test_adds_deletes_renames_copies_and_untracked_are_paired_right(
        self, repo: _RepoHelper
    ) -> None:
        # one of each kind that change_files pairs: modify, rename, delete, add, untracked.
        (repo / "a.txt").write_text("edited\n", encoding="utf-8")  # modify
        repo.git("mv", "b.txt", "c.txt")  # rename
        (repo / "e.txt").write_text("tracked add\n", encoding="utf-8")
        repo.git("add", "e.txt")  # tracked add
        repo.git("rm", "-q", "to_delete.txt")  # delete
        (repo / "untracked.txt").write_text("outside git\n", encoding="utf-8")  # untracked
        pairs = scope.change_files(self._base(repo), cwd=repo.root)
        assert ("a.txt", "a.txt") in pairs
        assert ("b.txt", "c.txt") in pairs
        assert ("to_delete.txt", None) in pairs
        assert (None, "e.txt") in pairs
        assert (None, "untracked.txt") in pairs

    def test_show_at_reads_bytes_at_the_base_and_returns_none_for_absent(
        self, repo: _RepoHelper
    ) -> None:
        assert scope.show_at(self._base(repo), "a.txt", cwd=repo.root) == "one\n"
        assert scope.show_at(self._base(repo), "nope.txt", cwd=repo.root) is None
