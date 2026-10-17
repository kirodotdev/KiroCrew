"""Diff-scope degradation — the three ways ``scoped_relpaths`` answers "unscoped".

``None`` and ``set()`` are NOT interchangeable here: ``None`` widens the edit fence to
the whole repository, ``set()`` narrows it to nothing. The module's own docstring
records a regression where a valid-but-empty diff collapsed into ``None`` and silently
un-fenced the run, so each way of reaching ``None`` (blank ref, git raising, git
exiting non-zero) is pinned separately, and the empty-but-successful diff is pinned as
a set. ``git`` is injected via the ``runner`` seam — no subprocess runs.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from kiro_crew.apps.builtins.auto_improvement.spine.scope import in_scope, scoped_relpaths
from kiro_crew.subprocess_utf8 import UTF8_TEXT


def _proc(*, returncode: int = 0, stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")


class TestUnscopedDegradations:
    def test_a_blank_base_ref_is_unscoped_without_shelling_git(self, tmp_path: Path) -> None:
        """No ref means the operator set no scope, so git must not even be invoked."""
        calls: list[list[str]] = []

        def _runner(argv, **kwargs):
            calls.append(list(argv))
            return _proc()

        for blank in ("", "   ", None):
            assert scoped_relpaths(tmp_path, blank, runner=_runner) is None  # type: ignore[arg-type]
        assert calls == []

    def test_a_raising_git_degrades_to_unscoped_instead_of_crashing(self, tmp_path: Path) -> None:
        def _runner(argv, **kwargs):
            raise OSError("git not on PATH")

        assert scoped_relpaths(tmp_path, "origin/main", runner=_runner) is None

    def test_a_nonzero_git_exit_degrades_to_unscoped(self, tmp_path: Path) -> None:
        """An unresolvable base ref cannot yield a scope, so it must not yield an
        empty one — that would fence the run down to editing nothing."""
        out = scoped_relpaths(tmp_path, "no/such/ref", runner=lambda argv, **kw: _proc(returncode=128))
        assert out is None


class TestSuccessfulDiff:
    def test_a_successful_empty_diff_is_a_scope_of_nothing_not_unscoped(
        self, tmp_path: Path
    ) -> None:
        """The regression the module docstring records: ``set()`` must survive as a set."""
        out = scoped_relpaths(tmp_path, "HEAD", runner=lambda argv, **kw: _proc(stdout="\n \n"))
        assert out == set()
        assert out is not None

    def test_it_diffs_three_dot_against_the_base_in_the_clone(self, tmp_path: Path) -> None:
        seen: dict[str, object] = {}

        def _runner(argv, **kwargs):
            seen["argv"] = list(argv)
            seen["kwargs"] = kwargs
            return _proc(stdout="src/a.py\n src/b.py \n")

        assert scoped_relpaths(tmp_path, " feat/base ", runner=_runner) == {
            "src/a.py",
            "src/b.py",
        }
        argv = seen["argv"]
        assert argv[:3] == ["git", "-C", str(tmp_path)]  # type: ignore[index]
        assert "feat/base...HEAD" in argv  # type: ignore[operator]
        assert "--name-only" in argv  # type: ignore[operator]
        assert seen["kwargs"]["timeout"] == 60  # type: ignore[index]


class TestInScopePredicate:
    def test_unscoped_admits_every_path(self) -> None:
        assert in_scope("anything/at/all.py", None) is True

    def test_membership_is_verbatim(self) -> None:
        scope = {"src/a.py"}
        assert in_scope("src/a.py", scope) is True
        assert in_scope("src/b.py", scope) is False

    def test_a_scope_of_nothing_admits_nothing(self) -> None:
        assert in_scope("src/a.py", set()) is False


class TestGitOutputIsDecodedAsUtf8:
    """The scope set and the gate that consumes it must share one decoder.

    ``scoped_relpaths`` passes ``core.quotePath=false``, which is what makes git emit
    a non-ASCII path as RAW UTF-8 bytes instead of its default octal escapes. Decoding
    those bytes with ``locale.getpreferredencoding(False)`` -- what a bare ``text=True``
    selects -- is the legacy ANSI code page on Windows, so the two sides of ``in_scope``
    stop agreeing:

    * a strict-decode failure raises inside the ``runner`` call, and the module's
      ``except Exception`` turns that into ``None`` -- which is UNSCOPED, widening the
      edit fence to the whole repository. That is the exact fail-open direction this
      module's docstring records having already fixed once, by another route.
    * a code page that maps every byte (cp1252) raises nothing and yields a mojibake
      path instead, so a file that IS in the branch's change set is judged out of
      scope and the agent's edit to it is rejected.

    ``spine/gate.py::_changed_status_paths`` -- which produces the paths compared
    against this set -- already splats ``UTF8_TEXT``. Pinning the producer to the same
    mapping makes an undecodable byte become U+FFFD on BOTH sides, so the two still
    compare equal.

    ``scripts/check_subprocess_encoding.py`` cannot catch this site: it matches spawn
    functions BY NAME, and the call here goes through the injected ``runner`` seam.
    """

    def test_the_diff_is_decoded_as_utf8_not_the_host_code_page(self, tmp_path: Path) -> None:
        seen: dict[str, object] = {}

        def _runner(argv, **kwargs):
            seen.update(kwargs)
            return _proc(stdout="src/a.py")

        assert scoped_relpaths(tmp_path, "origin/main", runner=_runner) == {"src/a.py"}
        assert seen["encoding"] == "utf-8"
        assert seen["errors"] == "replace"

    def test_it_uses_the_shared_utf8_mapping_rather_than_a_local_spelling(
        self, tmp_path: Path
    ) -> None:
        """One definition of "decode this child as UTF-8", not a re-typed pair."""
        seen: dict[str, object] = {}

        def _runner(argv, **kwargs):
            seen.update(kwargs)
            return _proc(stdout="")

        scoped_relpaths(tmp_path, "origin/main", runner=_runner)
        assert {k: seen.get(k) for k in UTF8_TEXT} == dict(UTF8_TEXT)

    def test_a_non_ascii_path_survives_into_the_scope_set(self, tmp_path: Path) -> None:
        """The set must hold the path the gate will later compare against, verbatim."""
        non_ascii = "src/日本語/café.py"

        def _runner(argv, **kwargs):
            return _proc(stdout=non_ascii + "\n" + "src/a.py" + "\n")

        scope = scoped_relpaths(tmp_path, "origin/main", runner=_runner)

        assert scope == {non_ascii, "src/a.py"}
        assert in_scope(non_ascii, scope) is True

    def test_quote_path_stays_off_so_the_bytes_are_raw_utf8(self, tmp_path: Path) -> None:
        """The decoder pin is only correct while git is told not to escape paths."""
        seen: dict[str, object] = {}

        def _runner(argv, **kwargs):
            seen["argv"] = list(argv)
            return _proc(stdout="")

        scoped_relpaths(tmp_path, "origin/main", runner=_runner)

        assert "core.quotePath=false" in seen["argv"]  # type: ignore[operator]
