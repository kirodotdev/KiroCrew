"""Contract tests for the SHADOW skip-tier scorer, scripts/ci_test_selection.py.

Pinned: each tier's signal, the `select` fall-backs (every file in A, verdict
`full`), a missing bundle table holding files out of G, and every failure (no
bundle, a corrupt one, the time-out) giving verdict `full` with exit 0.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import threading
from pathlib import Path

import pytest

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from scripts import ci_test_map as ctm
from scripts import ci_test_selection as cts

BUNDLE = {
    "schema": 1,
    "commit": "abc",
    "tests": {"test/test_narrow.py": 2, "test/test_w1.py": 1, "test/test_w2.py": 1},
    "files": {
        "src/kiro_crew/acp/mod.py": [0],
        "src/kiro_crew/acp/wide.py": [1, 2],
        "test/sub/helpers.py": [0],
    },
    "cochange": {"src/kiro_crew/acp/mod.py": ["test/test_cochanged.py"]},
}
UNIVERSE = [
    "test/test_narrow.py",
    "test/test_w1.py",
    "test/test_w2.py",
    "test/test_named.py",
    "test/sub/test_near.py",
    "test/test_cochanged.py",
    "test/test_acp_thing.py",
    "test/test_other.py",
    "test/test_edited.py",
]
GUARD = ["test/test_gate.py"]
DIFF = (
    "M\tsrc/kiro_crew/acp/mod.py\nM\tsrc/kiro_crew/acp/wide.py\n"
    "M\ttest/sub/helpers.py\nM\ttest/test_edited.py\n"
)


def _score(diff: str = DIFF, bundle: dict = BUNDLE, **kw: object) -> dict:
    def grep(modules: list[str]) -> set[str]:
        changed = ["kiro_crew.acp.mod", "kiro_crew.acp.wide", "sub.helpers", "test_edited"]
        assert modules == changed
        return {"test/test_named.py"}

    args: dict = {
        "skippable": {"G"},
        "narrow_reach_max": 1,
        "grep": grep,
        **kw,
    }
    return cts.score(ctm.parse_name_status(diff), bundle, UNIVERSE, GUARD, **args)


def test_each_tier_takes_the_first_signal_that_matches() -> None:
    verdict = _score()
    assert verdict["tiers"] == {
        "A": ["test/test_edited.py", "test/test_gate.py"],
        "B": ["test/test_narrow.py"],
        "C": ["test/test_w1.py", "test/test_w2.py"],
        "D": ["test/test_named.py"],
        "E": ["test/sub/test_near.py"],
        "F": ["test/test_acp_thing.py", "test/test_cochanged.py"],
        "G": ["test/test_other.py"],
    }
    assert verdict["verdict"] == "select" and verdict["reason"] is None
    assert verdict["would_skip"] == ["test/test_other.py"]
    # Weights: test_narrow counts 2 node ids, every file the map lacks counts 1.
    assert verdict["would_run_share"] == pytest.approx(10 / 11)


def test_no_skippable_tier_skips_nothing() -> None:
    verdict = _score(skippable=set())
    assert verdict["would_skip"] == [] and verdict["would_run_share"] == 1.0
    assert cts._verdict({t: [] for t in cts.TIERS}, [], {}, None)["would_run_share"] == 1.0
    assert cts._package("src/kiro_crew/__init__.py") is None
    assert cts._package("src/kiro_crew/session.py") == "session"


@pytest.mark.parametrize(
    "extra",
    [
        "A\tsrc/kiro_crew/new.py",
        "D\tsrc/kiro_crew/acp/mod.py",
        "R100\tsrc/kiro_crew/acp/mod.py\tsrc/kiro_crew/acp/m2.py",
        "M\ttest/conftest.py",
        "M\tsetup.cfg",
        "M\tsrc/kiro_crew/never_ran.py",
    ],
)
def test_every_select_fallback_puts_every_file_in_tier_a(extra: str) -> None:
    verdict = _score(DIFF + extra + "\n")
    assert verdict["verdict"] == "full" and verdict["reason"].startswith("fallback: ")
    assert verdict["tiers"]["A"] == sorted(UNIVERSE + GUARD)
    assert all(not verdict["tiers"][t] for t in "BCDEFG")
    assert verdict["would_skip"] == [] and verdict["would_run_share"] == 1.0


def test_an_empty_guard_tier_is_a_fallback() -> None:
    verdict = cts.score(
        ctm.parse_name_status(DIFF),
        BUNDLE,
        UNIVERSE,
        [],
        skippable={"G"},
        narrow_reach_max=1,
        grep=lambda modules: set(),
    )
    assert verdict["verdict"] == "full" and "guard tier" in verdict["reason"]


def test_a_missing_table_holds_would_be_g_files_in_f() -> None:
    bundle = {k: v for k, v in BUNDLE.items() if k != "cochange"}
    verdict = _score(bundle=bundle)
    assert verdict["tiers"]["G"] == []
    assert verdict["tiers"]["F"] == [
        "test/test_acp_thing.py",
        "test/test_cochanged.py",
        "test/test_other.py",
    ]
    assert verdict["verdict"] == "select" and verdict["would_skip"] == []


def test_a_held_file_is_never_skipped_even_when_f_is_skippable() -> None:
    bundle = {k: v for k, v in BUNDLE.items() if k != "cochange"}
    verdict = _score(bundle=bundle, skippable={"F", "G"})
    assert verdict["would_skip"] == ["test/test_acp_thing.py"]


def test_a_missing_map_table_falls_back() -> None:
    bundle = {k: v for k, v in BUNDLE.items() if k != "files"}
    assert _score(bundle=bundle)["verdict"] == "full"


def test_a_schema_2_bundle_loads_as_its_map_cochange_and_guard(tmp_path: Path) -> None:
    flat = {k: v for k, v in BUNDLE.items() if k != "cochange"}
    packed = {
        "schema": ctm.BUNDLE_SCHEMA,
        "map": flat,
        "co_change": {"src/kiro_crew/acp/mod.py": {"test/test_cochanged.py": 3}},
        "co_change_window": WHOLE,
        "guard": GUARD,
    }
    path = tmp_path / "bundle.json.gz"
    path.write_bytes(gzip.compress(json.dumps(packed).encode("utf-8")))
    assert cts.load_bundle(path) == {**BUNDLE, "guard": GUARD}
    for bad in ({**packed, "co_change": {"a": ["t"]}}, {**packed, "map": None}):
        path.write_bytes(gzip.compress(json.dumps(bad).encode("utf-8")))
        with pytest.raises(ValueError):
            cts.load_bundle(path)


NO_DATA = pytest.mark.parametrize("without", ["left out", None, {}, []])
WHOLE = {"asked": 2000, "read": 2000, "shallow": False}
PART = {"asked": 2000, "read": 50, "shallow": True}


def _packed(tmp_path: Path, table: str, without: object, flat: dict) -> Path:
    """A schema-2 file whose `table` is left out, or written as `without`."""
    packed = {
        "schema": ctm.BUNDLE_SCHEMA,
        "map": {k: v for k, v in flat.items() if k != "cochange"},
        "co_change": {"src/kiro_crew/acp/mod.py": {"test/test_cochanged.py": 3}},
        "co_change_window": WHOLE,
        "guard": GUARD,
    }
    del packed[table]
    if without != "left out":
        packed[table] = without
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps(packed), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("table", "without"),
    [
        *[("co_change", form) for form in ("left out", None, {}, [])],
        # The table is there, and its window does not say it covers what was asked.
        *[("co_change_window", form) for form in ("left out", None, {}, [], PART)],
    ],
)
def test_a_schema_2_bundle_with_no_or_part_of_the_co_change_data_skips_nothing(
    tmp_path: Path, table: str, without: object
) -> None:
    whole = _score(bundle=BUNDLE)
    assert whole["would_skip"] == whole["tiers"]["G"] != []
    try:
        bundle = cts.load_bundle(_packed(tmp_path, table, without, BUNDLE))
    except ValueError:
        # A table of another shape is corrupt: the CLI answers `full` for it.
        assert (table, without) == ("co_change", [])
        return
    assert "cochange" not in bundle
    verdict = _score(bundle=bundle)
    # Every file the full bundle would skip is held, with whatever co-change tied in.
    assert verdict["verdict"] == "select" and verdict["would_skip"] == verdict["tiers"]["G"] == []
    assert set(whole["tiers"]["F"] + whole["tiers"]["G"]) == set(verdict["tiers"]["F"])
    # A held file stays out of the skip list even when its tier is skippable.
    every = _score(bundle=bundle, skippable=set(cts.TIERS))
    assert set(whole["tiers"]["G"]).isdisjoint(every["would_skip"])


@NO_DATA
def test_a_schema_2_bundle_with_no_guard_data_runs_the_live_guard_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, without: object
) -> None:
    _isolate_git(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    _repo(repo, {"src/kiro_crew/testing/wait.py": "", "test/test_x.py": "", "test/test_g.py": ""})
    scans: list[Path] = []

    def live_scan(root: Path) -> list[str]:
        scans.append(root)
        return ["test/test_g.py"]

    monkeypatch.setattr(cts.ctm, "guard_tier", live_scan)
    flat = {**BUNDLE, "files": {"src/kiro_crew/testing/wait.py": [0]}}
    path = _packed(tmp_path, "guard", without, flat)
    code, verdict = _main(tmp_path, path.read_text(encoding="utf-8"), root=repo)
    assert code == 0
    if without == {}:
        # A table of another shape is corrupt, never a quietly lost signal.
        assert verdict["verdict"] == "full" and not scans
    else:
        assert verdict["reason"] is None, verdict["reason"]
        assert scans == [repo] and verdict["tiers"]["A"] == ["test/test_g.py"]


def test_bounded_returns_the_value_or_reraises_the_error() -> None:
    assert cts.bounded(lambda: 3, FALLBACK_TIMEOUT_SECS) == 3
    with pytest.raises(ZeroDivisionError):
        cts.bounded(lambda: 1 // 0, FALLBACK_TIMEOUT_SECS)


def test_module_names_and_grep_patterns() -> None:
    assert cts.module_name("src/kiro_crew/acp/mod.py") == "kiro_crew.acp.mod"
    assert cts.module_name("src/kiro_crew/acp/__init__.py") == "kiro_crew.acp"
    assert cts.module_name("test/acp_client_helpers.py") == "acp_client_helpers"
    assert cts.module_name("README.md") is None
    fixed, confirm, wrapped = cts.name_patterns(["kiro_crew.acp.mod", "helpers"])
    assert fixed == ["helpers", "kiro_crew.acp", "kiro_crew.acp.mod"]
    assert cts.name_patterns([]) == ([], re.compile(r"(?!)"), re.compile(r"(?!)"))
    assert wrapped.search("from kiro_crew.acp import (")
    assert wrapped.search("from  kiro_crew.acp \\")
    assert wrapped.search("from kiro_crew.acp import (  # deps")
    assert wrapped.search("from kiro_crew.acp import (other,  # deps")
    # Closed on its own line: the confirming regex reads it whole.
    assert not wrapped.search("from kiro_crew.acp import (other)  # (")
    # Only the changed module's own package is worth parsing a file for.
    assert not wrapped.search("from kiro_crew.acp.client import (")

    def hit(line: str) -> bool:
        return confirm.search(line) is not None

    assert hit("from kiro_crew.acp.mod import f")
    assert hit("from kiro_crew.acp import other, mod")
    # Any valid keyword spacing.
    assert hit("from kiro_crew.acp    import mod")
    assert hit("from kiro_crew.acp import\tmod")
    assert hit("from\tkiro_crew.acp import mod")
    assert hit("from kiro_crew.acp import(mod)")
    assert not hit("from kiro_crew.acp importer mod")
    assert hit('monkeypatch.setattr("kiro_crew.acp.mod.f", g)')
    assert hit("from helpers import x")
    assert not hit("from kiro_crew.acp.model import f")
    assert not hit("from kiro_crew.acp import modest")
    assert not hit("from test_helpers import x")


def test_git_grep_and_ls_files_read_the_tracked_test_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_git(tmp_path, monkeypatch)
    plain, tmp_path = tmp_path / "plain", tmp_path / "repo"
    plain.mkdir()
    (tmp_path / "test").mkdir(parents=True)
    (tmp_path / "test" / "test_x.py").write_text(
        "from kiro_crew.acp import mod\n", encoding="utf-8"
    )
    # A prefilter hit the regex then rejects.
    (tmp_path / "test" / "test_y.py").write_text(
        "from kiro_crew.acp import modest\n", encoding="utf-8"
    )
    (tmp_path / "test" / "helpers.py").write_text("", encoding="utf-8")
    # Wrapped imports: the leaf is on a later line than `from ... import`.
    (tmp_path / "test" / "test_wrapped.py").write_text(
        "from kiro_crew.acp import (\n    other,\n    mod,\n)\n", encoding="utf-8"
    )
    (tmp_path / "test" / "test_backslash.py").write_text(
        "from kiro_crew.acp import other, \\\n    mod\n", encoding="utf-8"
    )
    (tmp_path / "test" / "test_spaced.py").write_text(
        "from kiro_crew.acp    import\tmod\n", encoding="utf-8"
    )
    (tmp_path / "test" / "test_wrapped_spaced.py").write_text(
        "from  kiro_crew.acp \\\n    import mod\n", encoding="utf-8"
    )
    # A comment after the open parenthesis, or after a name on the opening line.
    (tmp_path / "test" / "test_commented.py").write_text(
        "from kiro_crew.acp import (  # deps\n    mod,\n)\n", encoding="utf-8"
    )
    (tmp_path / "test" / "test_commented_open.py").write_text(
        "from kiro_crew.acp import (other,  # deps\n    mod)\n", encoding="utf-8"
    )
    (tmp_path / "test" / "test_wrapped_other.py").write_text(
        "from kiro_crew.acp import (\n    modest,\n)\n", encoding="utf-8"
    )
    # Unparseable: the signal is kept rather than lost.
    (tmp_path / "test" / "test_broken.py").write_text(
        "from kiro_crew.acp import (\n", encoding="utf-8"
    )
    for argv in (["git", "init", "-q"], ["git", "add", "."]):
        subprocess.run(
            argv, cwd=tmp_path, check=True, capture_output=True, timeout=FALLBACK_TIMEOUT_SECS
        )
    assert cts.git_grep(["kiro_crew.acp.mod"], tmp_path) == {
        "test/test_backslash.py",
        "test/test_broken.py",
        "test/test_commented.py",
        "test/test_commented_open.py",
        "test/test_spaced.py",
        "test/test_wrapped.py",
        "test/test_wrapped_spaced.py",
        "test/test_x.py",
    }
    assert cts.git_grep(["nothing.here"], tmp_path) == set()
    assert cts.git_grep([], tmp_path) == set()
    assert cts.git_test_files(tmp_path) == [
        "test/test_backslash.py",
        "test/test_broken.py",
        "test/test_commented.py",
        "test/test_commented_open.py",
        "test/test_spaced.py",
        "test/test_wrapped.py",
        "test/test_wrapped_other.py",
        "test/test_wrapped_spaced.py",
        "test/test_x.py",
        "test/test_y.py",
    ]
    with pytest.raises(subprocess.CalledProcessError):
        cts.git_grep(["x"], plain)


def test_checked_in_rates_make_only_g_skippable() -> None:
    assert cts.load_rates(cts.RATES) == ({"G"}, 20)


def _isolate_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No host git config (signing, hooks, templates) reaches a temp repo, and no
    # repository above `tmp_path` is found wherever the host's tmp dir sits.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def _repo(root: Path, *commits: dict[str, str]) -> list[str]:
    """A git repository at `root` with one commit per `{path: text}`; their SHAs."""
    shas = []
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    for files in commits:
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        for argv in (["init", "-q"], ["add", "."], [*identity, "commit", "-qm", "c"]):
            subprocess.run(
                ["git", *argv],
                cwd=root,
                check=True,
                capture_output=True,
                timeout=FALLBACK_TIMEOUT_SECS,
            )
        shas.append(cts.rev_parse("HEAD", root))
    return shas


def _main(
    tmp_path: Path, bundle_text: str | None, *extra: str, root: Path | None = None
) -> tuple[int, dict]:
    bundle = tmp_path / "bundle.json"
    if bundle_text is not None:
        bundle.write_text(bundle_text, encoding="utf-8")
    diff = tmp_path / "diff.txt"
    diff.write_text("M\tsrc/kiro_crew/testing/wait.py\n", encoding="utf-8")
    out = tmp_path / "verdict.json"
    argv = ["--root", str(root or tmp_path), "--bundle", str(bundle), "--name-status", str(diff)]
    code = cts.main([*argv, "--out", str(out), *extra])
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_cli_writes_a_select_verdict_for_a_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_git(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    _repo(
        repo,
        {
            "src/kiro_crew/testing/wait.py": "",
            "test/metrics/test_schema.py": "",
            "test/test_gate.py": "",
            "test/test_commented.py": "from kiro_crew.testing import (  # deps\n    wait,\n)\n",
            "test/test_other.py": "",
        },
    )
    bundle = {
        "schema": 1,
        "commit": "c0ffee",
        "tests": {"test/metrics/test_schema.py": 3},
        "files": {"src/kiro_crew/testing/wait.py": [0]},
        "cochange": {},
        # Precomputed: the live guard scan does not run.
        "guard": ["test/test_gate.py"],
    }
    code, verdict = _main(tmp_path, json.dumps(bundle), root=repo)
    assert code == 0
    assert set(verdict) == {
        "schema",
        "base",
        "head",
        "bundle_commit",
        "verdict",
        "reason",
        "tiers",
        "would_skip",
        "would_run_share",
    }
    assert verdict["schema"] == 1 and verdict["bundle_commit"] == "c0ffee"
    assert verdict["base"] is None and verdict["head"] is None
    assert verdict["verdict"] == "select" and verdict["reason"] is None
    assert verdict["tiers"]["A"] == ["test/test_gate.py"]
    assert verdict["tiers"]["B"] == ["test/metrics/test_schema.py"]
    # A commented, wrapped import of the changed module is a signal, not G.
    assert verdict["tiers"]["D"] == ["test/test_commented.py"]
    assert verdict["tiers"]["G"] == verdict["would_skip"] == ["test/test_other.py"]
    assert verdict["would_run_share"] == pytest.approx(5 / 6)


def test_without_a_guard_table_the_live_guard_scan_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_git(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    _repo(repo, {"src/kiro_crew/testing/wait.py": "", "test/test_x.py": ""})
    bundle = {**BUNDLE, "files": {"src/kiro_crew/testing/wait.py": [0]}}
    code, verdict = _main(tmp_path, json.dumps(bundle), root=repo)
    # The scan of this tree finds no test reading the test tree.
    assert code == 0 and verdict["verdict"] == "full"
    assert verdict["reason"] == "fallback: guard tier is empty: refusing to reduce"


@pytest.mark.parametrize(
    "bundle_text",
    [
        None,
        "{not json",
        json.dumps({"schema": 99}),
        json.dumps([1]),
        # A present table of the wrong shape is corrupt, not missing.
        json.dumps({**BUNDLE, "cochange": {"src/kiro_crew/acp/mod.py": "test/test_x.py"}}),
        json.dumps({**BUNDLE, "cochange": {"src/kiro_crew/acp/mod.py": [1]}}),
        json.dumps({**BUNDLE, "cochange": []}),
        json.dumps({**BUNDLE, "files": {"src/kiro_crew/acp/mod.py": [3]}}),
        json.dumps({**BUNDLE, "files": {"src/kiro_crew/acp/mod.py": 0}}),
        json.dumps({**BUNDLE, "files": {"src/kiro_crew/acp/mod.py": [True]}}),
        json.dumps({**BUNDLE, "tests": {"test/test_narrow.py": "2"}}),
        json.dumps({**BUNDLE, "tests": {"test/test_narrow.py": -1}}),
        json.dumps({**BUNDLE, "tests": ["test/test_narrow.py"]}),
        json.dumps({**BUNDLE, "guard": "test/test_gate.py"}),
        json.dumps({**BUNDLE, "guard": [1]}),
    ],
)
def test_no_or_a_corrupt_bundle_is_a_full_verdict_and_exit_0(
    tmp_path: Path, bundle_text: str | None
) -> None:
    code, verdict = _main(tmp_path, bundle_text)
    assert code == 0 and verdict["verdict"] == "full" and verdict["reason"]
    assert verdict["would_skip"] == [] and verdict["would_run_share"] == 1.0
    assert set(verdict["tiers"]) == set(cts.TIERS)


def test_the_time_out_abandons_unfinished_work_with_a_full_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release, finished = threading.Event(), threading.Event()

    def stuck(args: argparse.Namespace, grep: cts.Grep) -> tuple[None, dict]:
        try:
            release.wait(FALLBACK_TIMEOUT_SECS)
        finally:
            finished.set()
        return None, cts.full_verdict("never reached")

    monkeypatch.setattr(cts, "_score_args", stuck)
    try:
        code, verdict = _main(tmp_path, json.dumps(BUNDLE), "--timeout", "0.05")
    finally:
        release.set()
        assert finished.wait(FALLBACK_TIMEOUT_SECS)
    assert code == 0 and verdict["verdict"] == "full"
    assert verdict["reason"] == "TimeoutError: scoring did not finish within 0.05s"


def test_cli_usage_and_an_unwritable_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _isolate_git(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    _repo(repo, {"test/test_x.py": ""})
    bundle = tmp_path / "b.json"
    assert cts.main(["--bundle", str(bundle), "--out", str(tmp_path / "v.json")]) == 2
    bundle.write_text(json.dumps({**BUNDLE, "guard": ["test/test_gate.py"]}), encoding="utf-8")
    out = tmp_path / "missing-dir" / "v.json"
    argv = ["--root", str(repo), "--bundle", str(bundle), "--out", str(out)]
    assert cts.main([*argv, "--base", "HEAD", "--head", "HEAD"]) == 0 and not out.exists()


def test_a_head_other_than_the_checkout_is_a_full_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_git(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    first, second = _repo(
        repo, {"test/test_x.py": "from kiro_crew.acp import mod\n"}, {"test/test_x.py": "\n"}
    )
    bundle = tmp_path / "b.json"
    bundle.write_text(json.dumps({**BUNDLE, "guard": ["test/test_gate.py"]}), encoding="utf-8")
    out = tmp_path / "v.json"
    # HEAD~1 still imports the module; its test tree is not the checkout's.
    argv = ["--root", str(repo), "--bundle", str(bundle), "--out", str(out)]
    assert cts.main([*argv, "--base", "HEAD~1", "--head", "HEAD~1"]) == 0
    verdict = json.loads(out.read_text(encoding="utf-8"))
    assert verdict["verdict"] == "full" and f"not --head {first}" in verdict["reason"]
    assert verdict["would_skip"] == []
    assert cts.main([*argv, "--base", "HEAD~1", "--head", "HEAD"]) == 0
    verdict = json.loads(out.read_text(encoding="utf-8"))
    # The revisions are written as the commit SHAs they name.
    assert (verdict["base"], verdict["head"]) == (first, second)
    assert verdict["reason"] is None and verdict["tiers"]["A"] == [
        "test/test_gate.py",
        "test/test_x.py",
    ]


def test_a_revision_that_names_no_commit_is_a_full_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_git(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    _repo(repo, {"test/test_x.py": ""})
    out = tmp_path / "v.json"
    argv = ["--root", str(repo), "--bundle", str(tmp_path / "b.json"), "--out", str(out)]
    assert cts.main([*argv, "--base", "nope", "--head", "HEAD"]) == 0
    verdict = json.loads(out.read_text(encoding="utf-8"))
    assert verdict["verdict"] == "full" and verdict["reason"].startswith("CalledProcessError")
    assert (verdict["base"], verdict["head"]) == ("nope", "HEAD")
