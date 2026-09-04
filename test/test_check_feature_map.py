"""Unit tests for scripts/check_feature_map.py.

The gate's whole value is where it draws the line: structural changes (a page or
handler file appearing or disappearing, a route entry arriving or leaving) demand
a map update, and edits never do. Both halves need pinning. A rule that silently
widens turns every UI fix into a map review, which is how a documentation gate
gets ignored; a rule that silently narrows lets features land unmapped, which is
the failure it exists to prevent.

``classify`` is pure, so the trigger matrix is tested without a repository. The
git-reading shell gets its own throwaway repos, because the rename handling and
the fail-open posture only exist below that seam.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "scripts", "check_feature_map.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_feature_map", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_feature_map"] = module
    spec.loader.exec_module(module)
    return module


gate = _load()

PAGE = "website/src/pages/NewThing.tsx"
NESTED_PAGE = "website/src/pages/settings/NewPanel.tsx"
HANDLER = "src/kiro_crew/dashboard/handlers/new_thing.py"


# ---------------------------------------------------------------------------
# The trigger matrix
# ---------------------------------------------------------------------------


class TestTriggers:
    """A structural change without a map edit is the only failing shape."""

    @pytest.mark.parametrize(
        "added,deleted,routes_added,routes_removed",
        [
            pytest.param([PAGE], [], 0, 0, id="page-added"),
            pytest.param([NESTED_PAGE], [], 0, 0, id="nested-page-added"),
            pytest.param([], ["website/src/pages/OldThing.tsx"], 0, 0, id="page-deleted"),
            pytest.param([HANDLER], [], 0, 0, id="handler-added"),
            pytest.param(
                [], ["src/kiro_crew/dashboard/handlers/old_thing.py"], 0, 0, id="handler-deleted"
            ),
            pytest.param([], [], 1, 0, id="route-added"),
            pytest.param([], [], 0, 1, id="route-removed"),
            pytest.param([], [], 1, 1, id="route-swapped"),
            pytest.param([PAGE], [], 1, 0, id="page-and-route"),
        ],
    )
    def test_fails_without_a_map_edit(self, added, deleted, routes_added, routes_removed):
        assert not gate.classify(added, deleted, routes_added, routes_removed, False).ok

    @pytest.mark.parametrize(
        "added,deleted,routes_added,routes_removed",
        [
            pytest.param([PAGE], [], 0, 0, id="page-added"),
            pytest.param([], ["website/src/pages/OldThing.tsx"], 0, 0, id="page-deleted"),
            pytest.param([HANDLER], [], 0, 0, id="handler-added"),
            pytest.param([], [], 1, 0, id="route-added"),
            pytest.param([], [], 1, 1, id="route-swapped"),
            pytest.param([PAGE], [], 1, 0, id="page-and-route"),
        ],
    )
    def test_passes_when_the_map_is_edited(self, added, deleted, routes_added, routes_removed):
        assert gate.classify(added, deleted, routes_added, routes_removed, True).ok

    def test_edit_only_diff_passes(self):
        """The rule that makes the gate liveable: edits are not features.

        ``name_status`` puts modifications in neither list, so an edit-only diff
        reaches ``classify`` with both empty and no route entry moving either way.
        """
        assert gate.classify([], [], 0, 0, False).ok

    def test_a_swapped_route_still_demands_a_map_edit(self):
        """One destination replacing another is a structural change, not a wash.

        The counts used to be a single signed delta, so a swap arrived as zero and
        the gate read it as "nothing happened" — while the retired destination's
        row still claimed it existed and the new destination had no row at all.
        Netting is only safe if a route entry is fungible, and it is not: the map
        keys on which destination, never on how many.
        """
        assert not gate.classify([], [], 1, 1, False).ok
        assert gate.classify([], [], 1, 1, True).ok


class TestNonFeatures:
    """Files under a watched tree that are not destinations."""

    @pytest.mark.parametrize(
        "path",
        [
            "website/src/pages/NewThing.test.tsx",
            "website/src/pages/NewThing.spec.tsx",
            "website/src/pages/chat/helpers.test.ts",
            "website/src/pages/__snapshots__/NewThing.snap",
            "src/kiro_crew/dashboard/handlers/__init__.py",
            "src/kiro_crew/dashboard/handlers/_shared.py",
            "website/src/pages/overview/index.ts",
        ],
    )
    def test_ignored_inside_watched_trees(self, path):
        assert not gate.is_feature_file(path)
        assert gate.classify([path], [], 0, 0, False).ok

    @pytest.mark.parametrize(
        "path",
        [
            "website/src/components/NewThing.tsx",
            "website/src/surfaces/builtins.tsx",
            "src/kiro_crew/dashboard/chat_handlers.py",
            "src/kiro_crew/memory.py",
            "docs/guides/new.md",
            "test/test_new_thing.py",
        ],
    )
    def test_outside_watched_trees(self, path):
        assert not gate.is_feature_file(path)
        assert gate.classify([path], [], 0, 0, False).ok

    def test_real_page_is_a_feature_file(self):
        """Guard against a prefix typo that would exempt the whole tree."""
        assert gate.is_feature_file("website/src/pages/ChatPage.tsx")
        assert gate.is_feature_file("src/kiro_crew/dashboard/handlers/artifacts.py")

    def test_the_map_is_never_its_own_trigger(self):
        assert not gate.is_feature_file(gate.MAP_PATH)


class TestEvidence:
    """A failure must name what caused it, or the reader goes back to the diff."""

    def test_verdict_carries_the_triggering_paths(self):
        verdict = gate.classify([PAGE], ["website/src/pages/Old.tsx"], 2, 1, False)
        assert verdict.added == (PAGE,)
        assert verdict.deleted == ("website/src/pages/Old.tsx",)
        assert verdict.routes_added == 2
        assert verdict.routes_removed == 1

    def test_report_prints_paths_and_both_route_directions(self, capsys):
        gate.report(gate.classify([PAGE], [], 3, 1, False))
        out = capsys.readouterr().out
        assert PAGE in out
        assert "+3" in out
        assert "-1" in out
        assert gate.MAP_PATH in out

    def test_report_names_a_swap_in_both_directions(self, capsys):
        """A swap is the shape a netted figure erased, so it must reach the reader.

        Reporting only one direction would describe the swap as a plain addition
        and send the reader looking for one stale row instead of two.
        """
        gate.report(gate.classify([], [], 1, 1, False))
        out = capsys.readouterr().out
        assert "+1 / -1" in out
        assert gate.ROUTER_PATH in out

    def test_report_drops_non_features_from_the_evidence(self):
        verdict = gate.classify([PAGE, "website/src/pages/X.test.tsx"], [], 0, 0, False)
        assert verdict.added == (PAGE,)


# ---------------------------------------------------------------------------
# The git-reading shell
# ---------------------------------------------------------------------------


@pytest.mark.xdist_group(name="subprocess_spawn")
class TestAgainstRealGit:
    """End-to-end through throwaway repos built under ``tmp_path``."""

    @staticmethod
    def _repo(tmp_path) -> str:
        root = str(tmp_path / "repo")
        os.makedirs(os.path.join(root, "scripts"))
        run = lambda *a: subprocess.run(a, cwd=root, check=True, capture_output=True)  # noqa: E731
        run("git", "init", "-q", "-b", "main")
        run("git", "config", "user.email", "t@example.com")
        run("git", "config", "user.name", "t")
        # The gate loads scripts/ratchet_scope.py by a path relative to itself,
        # so the throwaway repo needs both files or every run dies at the loader
        # instead of testing the rule.
        for name in ("check_feature_map.py", "ratchet_scope.py"):
            with open(os.path.join(_REPO_ROOT, "scripts", name), encoding="utf-8") as src:
                body = src.read()
            with open(os.path.join(root, "scripts", name), "w", encoding="utf-8") as fh:
                fh.write(body)
        return root

    @staticmethod
    def _write(root: str, rel: str, body: str) -> None:
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(body)

    @staticmethod
    def _commit(root: str, msg: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", msg], cwd=root, check=True, capture_output=True
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            check=True,
        ).stdout.strip()

    @classmethod
    def _seed(cls, tmp_path) -> tuple[str, str]:
        """A repo with a map, a router and one page. Returns ``(root, base_sha)``."""
        root = cls._repo(tmp_path)
        cls._write(root, gate.MAP_PATH, "# Feature Map\n\n| a | b |\n")
        cls._write(root, gate.ROUTER_PATH, '<Route path="/chat" />\n')
        cls._write(root, "website/src/pages/ChatPage.tsx", "export default function C() {}\n")
        return root, cls._commit(root, "seed")

    @staticmethod
    def _run(root: str, base: str | None) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        if base:
            env["FEATURE_MAP_BASE_REF"] = base
        else:
            env.pop("FEATURE_MAP_BASE_REF", None)
        return subprocess.run(
            [sys.executable, os.path.join(root, "scripts", "check_feature_map.py")],
            cwd=root,
            env=env,
            capture_output=True,
            # The gate writes UTF-8 deliberately; `text=True` would decode with
            # the locale's preferred encoding (cp1252 on Windows) and mangle a
            # non-ASCII path in a finding.
            encoding="utf-8",
            errors="replace",
        )

    def test_added_page_without_map_fails(self, tmp_path):
        root, base = self._seed(tmp_path)
        self._write(root, PAGE, "export default function N() {}\n")
        result = self._run(root, base)
        assert result.returncode == 1
        assert PAGE in result.stdout

    def test_added_page_with_map_passes(self, tmp_path):
        root, base = self._seed(tmp_path)
        self._write(root, PAGE, "export default function N() {}\n")
        self._write(root, gate.MAP_PATH, "# Feature Map\n\n| a | b |\n| NewThing | x |\n")
        result = self._run(root, base)
        assert result.returncode == 0

    def test_renamed_page_without_map_fails(self, tmp_path):
        """A rename is the same staleness as a delete plus an add.

        This overturns an earlier reading that exempted renames because "a rename
        is the same destination under a new filename". True of the feature, false
        of the map: its page column cites the FILENAME, so after a rename that row
        points at a file which no longer exists, and the row for the file that does
        exist is missing. That is exactly the staleness the gate exists to catch,
        and the fix is a one-cell edit. The refactor exemption still holds for pure
        edits, which change no cited cell.

        Rename detection now affects only the reporting, not the verdict: without
        it git reports a delete plus an add, which fires too.
        """
        root, base = self._seed(tmp_path)
        subprocess.run(
            ["git", "mv", "website/src/pages/ChatPage.tsx", PAGE],
            cwd=root,
            check=True,
            capture_output=True,
        )
        result = self._run(root, base)
        assert result.returncode == 1
        assert PAGE in result.stdout
        assert "website/src/pages/ChatPage.tsx" in result.stdout

    def test_renamed_page_with_map_passes(self, tmp_path):
        root, base = self._seed(tmp_path)
        subprocess.run(
            ["git", "mv", "website/src/pages/ChatPage.tsx", PAGE],
            cwd=root,
            check=True,
            capture_output=True,
        )
        self._write(root, gate.MAP_PATH, "# Feature Map\n\n| a | b |\n| NewThing | x |\n")
        assert self._run(root, base).returncode == 0

    def test_edit_only_diff_passes(self, tmp_path):
        root, base = self._seed(tmp_path)
        self._write(root, "website/src/pages/ChatPage.tsx", "export default function C() { }\n")
        result = self._run(root, base)
        assert result.returncode == 0

    def test_deleted_handler_without_map_fails(self, tmp_path):
        root, base = self._repo(tmp_path), None
        self._write(root, gate.MAP_PATH, "# Feature Map\n")
        self._write(root, gate.ROUTER_PATH, '<Route path="/chat" />\n')
        self._write(root, HANDLER, "async def api_new(): ...\n")
        base = self._commit(root, "seed")
        os.remove(os.path.join(root, HANDLER))
        result = self._run(root, base)
        assert result.returncode == 1
        assert HANDLER in result.stdout

    def test_added_route_without_map_fails(self, tmp_path):
        root, base = self._seed(tmp_path)
        self._write(root, gate.ROUTER_PATH, '<Route path="/chat" />\n<Route path="/newthing" />\n')
        result = self._run(root, base)
        assert result.returncode == 1
        assert gate.ROUTER_PATH in result.stdout

    def test_swapped_route_without_map_fails(self, tmp_path):
        """The netting bug, end to end: one route out, one in, total unchanged.

        ``router_delta`` counts the ``+``/``-`` lines of the router's own diff
        rather than differencing two file totals, and that is what makes this
        fail. A total-count difference is zero here, so the gate used to pass a
        change that left the map's rows for BOTH destinations wrong — the retired
        one still listed, the new one absent. A ``classify`` test cannot catch a
        regression here, because the netting lived on this side of the seam too.
        """
        root, base = self._seed(tmp_path)
        self._write(root, gate.ROUTER_PATH, '<Route path="/sessions" />\n')
        result = self._run(root, base)
        assert result.returncode == 1
        assert gate.ROUTER_PATH in result.stdout
        assert "+1 / -1" in result.stdout

    def test_missing_base_fails_open(self, tmp_path):
        """A git edge case must not block a PR — see the gate's module docstring."""
        root, _base = self._seed(tmp_path)
        self._write(root, PAGE, "export default function N() {}\n")
        result = self._run(root, "0" * 40)
        assert result.returncode == 0
        assert "not checked" in result.stdout

    def test_deleted_router_without_map_fails(self, tmp_path):
        """Deleting the router retires every destination it registered.

        This used to fail OPEN, but only as an artifact of counting routes through
        ``open()``: an absent file raised, and the gate waved the change through.
        ``git diff`` reads a deletion perfectly well, and a diff that removes every
        route in the app is the largest structural change there is. The genuine
        fail-open case is git being unable to answer at all — a shallow clone or a
        missing base commit — which ``test_missing_base_fails_open`` covers.
        """
        root, base = self._seed(tmp_path)
        os.remove(os.path.join(root, gate.ROUTER_PATH))
        result = self._run(root, base)
        assert result.returncode == 1
        assert "+0 / -1" in result.stdout

    def test_no_base_ref_surveys_without_enforcing(self, tmp_path):
        root, _base = self._seed(tmp_path)
        self._write(root, PAGE, "export default function N() {}\n")
        result = self._run(root, None)
        assert result.returncode == 0
        assert "Not enforced here" in result.stdout

    def test_self_test_mode_passes(self, tmp_path):
        root, _base = self._seed(tmp_path)
        result = subprocess.run(
            [sys.executable, os.path.join(root, "scripts", "check_feature_map.py"), "--test"],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0, result.stdout
        assert "self-test passed" in result.stdout


class TestNameStatusParsing:
    """The ``-z`` record parser, which a quoted path would otherwise skip."""

    @pytest.mark.xdist_group(name="subprocess_spawn")
    def test_add_delete_and_rename_land_in_the_right_buckets(self, tmp_path, monkeypatch):
        root = TestAgainstRealGit._repo(tmp_path)
        TestAgainstRealGit._write(root, "keep.txt", "a\n")
        TestAgainstRealGit._write(root, "gone.txt", "b\n")
        TestAgainstRealGit._write(root, "moved.txt", "c" * 200 + "\n")
        base = TestAgainstRealGit._commit(root, "seed")

        TestAgainstRealGit._write(root, "fresh.txt", "d\n")
        os.remove(os.path.join(root, "gone.txt"))
        subprocess.run(
            ["git", "mv", "moved.txt", "elsewhere.txt"], cwd=root, check=True, capture_output=True
        )
        TestAgainstRealGit._write(root, "keep.txt", "a2\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)

        # Point the module's git plumbing at the throwaway repo rather than
        # re-importing it: monkeypatch restores REPO_ROOT even if an assertion
        # below raises, so a failure here cannot leak into another test.
        monkeypatch.setattr(gate, "REPO_ROOT", root)
        added, deleted, modified = gate.name_status(base)

        assert "fresh.txt" in added
        assert "gone.txt" in deleted
        # A rename is a delete plus an add: the map's row for the old filename is
        # stale and the new one has no row, so filing both paths as modifications
        # would read a renamed page as an edit and let the staleness through.
        assert "moved.txt" in deleted and "elsewhere.txt" in added
        assert "moved.txt" not in modified and "elsewhere.txt" not in modified
        assert "keep.txt" in modified

    @pytest.mark.xdist_group(name="subprocess_spawn")
    def test_untracked_file_counts_as_added(self, tmp_path, monkeypatch):
        """``git diff`` cannot see an untracked file, so a local run needs this.

        Without it the gate reports clean on a brand-new, not-yet-staged page —
        the exact change it exists to catch — and only CI would ever fail.
        """
        root = TestAgainstRealGit._repo(tmp_path)
        TestAgainstRealGit._write(root, "keep.txt", "a\n")
        base = TestAgainstRealGit._commit(root, "seed")
        TestAgainstRealGit._write(root, PAGE, "export default function N() {}\n")

        monkeypatch.setattr(gate, "REPO_ROOT", root)
        added, _deleted, _modified = gate.name_status(base)
        assert PAGE in added
