"""Contract tests for the verdict's `windows` block, scripts/ci_test_selection_windows.py.

Pinned: each part of the Windows set, every reason the verdict is `full` (the
Linux verdict, a bucket path read from ci.yml, an absent table, a stale bundle,
an error), the share, the summary line, and that the miss ledger reads every
block the scorer writes.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from macos_lane_helpers import load_workflow

from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS
from scripts import ci_test_selection as cts
from scripts import ci_test_selection_ledger as ledger

win = cts.windows
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
TESTS = {
    "test/test_lock.py": 4,
    "test/test_plain.py": 2,
    "test/test_win_only.py": 1,
    "test/test_named.py": 1,
    "test/test_other.py": 8,
    "test/test_gate.py": 1,
    "test/test_posix_only.py": 3,
}
INDEX = {name: at for at, name in enumerate(sorted(TESTS))}
BUNDLE: dict[str, Any] = {
    "schema": 1,
    "commit": "abc",
    "built_at": NOW.isoformat(),
    "tests": TESTS,
    "files": {
        "src/kiro_crew/lock.py": [INDEX["test/test_lock.py"], INDEX["test/test_posix_only.py"]],
        "src/kiro_crew/plain.py": [INDEX["test/test_plain.py"], INDEX["test/test_win_only.py"]],
        "src/kiro_crew/grown.py": [INDEX["test/test_named.py"]],
    },
    "cochange": {"src/kiro_crew/plain.py": ["test/test_other.py"]},
    "os_sensitive": {"src/kiro_crew/lock.py": "W2"},
    "windows_targeting": ["test/test_win_only.py", "test/test_posix_only.py"],
}
GUARD = ["test/test_gate.py"]
IGNORED = {"test/test_posix_only.py"}
BUCKET = ["**/*windows*", "setup.cfg"]


def _tiered(diff: win.Changes) -> dict[str, Any]:
    return cts.score(
        diff,
        BUNDLE,
        sorted(TESTS),
        GUARD,
        skippable={"G"},
        narrow_reach_max=20,
        grep=lambda m: set(),
    )


def _block(diff: str, **over: Any) -> dict[str, Any]:
    changes = cts.ctm.parse_name_status(diff)
    args: dict[str, Any] = {
        "added": {},
        "bucket": BUCKET,
        "ignored": IGNORED,
        "at": NOW,
        "score": _tiered,
        "bundle": BUNDLE,
        **over,
    }
    bundle = args.pop("bundle")
    linux = args.pop("linux", None) or cts.score(
        changes,
        bundle,
        sorted(TESTS),
        GUARD,
        skippable={"G"},
        narrow_reach_max=20,
        grep=lambda m: set(),
    )
    guard = args.pop("guard", GUARD)
    block = win.block(changes, linux, bundle, sorted(TESTS), guard, **args)
    # Every `select` runs the whole guard tier, less what Windows never collects.
    if block["verdict"] == "select":
        assert set(guard) - args["ignored"] <= set(block["run"])
        assert not args["ignored"] & set(block["run"])
    # The miss ledger, the block's reader, takes every block as it is written.
    parsed = ledger.parse_windows(json.loads(json.dumps(block)))
    assert parsed is not None and parsed["verdict"] == block["verdict"]
    assert parsed["run"] == set(block["run"]) and parsed["source"] == "shadow"
    assert set(block) == {"verdict", "reason", "run", "would_run_share"}
    return block


def _ad(diff: str) -> set[str]:
    body = _tiered(cts.ctm.parse_name_status(diff))
    return {test for tier in win.RUN_TIERS for test in body["tiers"][tier]}


def test_the_run_is_the_union_of_its_parts_and_always_holds_the_guard_tier() -> None:
    plain = "M\tsrc/kiro_crew/plain.py\n"
    lock = "M\tsrc/kiro_crew/lock.py\n"
    edited = "M\ttest/test_other.py\n"
    guard = set(GUARD)
    # The Windows-targeting files among the diff's tiers A to D, and the guard tier.
    assert set(_block(plain)["run"]) == {"test/test_win_only.py"} | guard
    assert "test/test_plain.py" in _ad(plain) - set(_block(plain)["run"])
    # A file the table names brings its own tiers A to D.
    assert set(_block(lock)["run"]) == _ad(lock) - IGNORED == {"test/test_lock.py"} | guard
    # A changed test file, whatever its tier.
    assert set(_block(edited)["run"]) == {"test/test_other.py"} | guard
    every = _block(plain + lock + edited)
    assert set(every["run"]) == (
        set(_block(plain)["run"]) | set(_block(lock)["run"]) | set(_block(edited)["run"])
    )
    assert every["verdict"] == "select" and every["reason"] is None
    assert every["run"] == sorted(every["run"])
    # The guard tier is the one the diff was scored with, whatever its size, and a
    # guard file Windows never collects is left out like any other.
    wide = [*GUARD, "test/test_named.py", "test/test_posix_only.py"]
    assert set(_block(plain, guard=wide)["run"]) == {"test/test_win_only.py", *wide} - IGNORED


def test_a_file_windows_never_collects_is_left_out_of_every_part() -> None:
    ignored = "test/test_posix_only.py"
    # As a changed test, and as a Windows-targeting test the table's file is linked to.
    for diff in (f"M\t{ignored}\n", "M\tsrc/kiro_crew/lock.py\n"):
        assert ignored not in _block(diff)["run"]
        assert ignored in _block(diff, ignored=set())["run"]


@pytest.mark.parametrize(
    "line",
    ["if sys.platform == 'win32':", "import fcntl", "subprocess.run(argv)", "x = 1  # os.fork"],
)
def test_an_added_line_that_hits_a_pattern_makes_its_file_os_sensitive(line: str) -> None:
    path = "src/kiro_crew/grown.py"
    diff = f"M\t{path}\n"
    assert cts.ost.tier_of(line) in ("W1", "W2", "W3")
    assert "test/test_named.py" in _block(diff, added={path: line + "\n"})["run"]
    # A line no pattern hits, and a hit in another file, leave it out.
    for added in ({path: "x = 1\n"}, {"src/kiro_crew/elsewhere.py": line + "\n"}, {}):
        assert "test/test_named.py" not in _block(diff, added=added)["run"]
    # With no added lines to read, every changed `.py` file counts.
    assert "test/test_named.py" in _block(diff, added=None)["run"]


def test_added_lines_are_read_per_file_from_a_zero_context_diff() -> None:
    diff = (
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\n"
        "-old\n+new\n++++ plus\n"
        "diff --git a/src/gone.py b/src/gone.py\n--- a/src/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
        "diff --git a/b.py b/b.py\n--- /dev/null\n+++ b/b.py\n@@ -0,0 +1 @@\n+only\n"
    )
    assert win.added_lines(diff) == {"src/a.py": "new\n+++ plus\n", "b.py": "only\n"}


@pytest.mark.parametrize("table", win.TABLES)
@pytest.mark.parametrize("without", ["left out", None, {}, []])
def test_an_absent_table_is_a_full_windows_verdict(table: str, without: object) -> None:
    bundle = {key: value for key, value in BUNDLE.items() if key != table}
    if without != "left out":
        bundle[table] = without
    block = _block("M\tsrc/kiro_crew/plain.py\n", bundle=bundle)
    assert block == win.full(f"table_absent: {table}")


@pytest.mark.parametrize(
    ("age", "stale"),
    [
        (timedelta(days=win.STALE_DAYS - 1), False),
        (timedelta(days=win.STALE_DAYS), False),
        (timedelta(days=win.STALE_DAYS, seconds=1), True),
        (timedelta(days=win.STALE_DAYS + 1), True),
        (timedelta(days=-2), False),
    ],
)
def test_a_bundle_older_than_the_limit_is_stale(age: timedelta, stale: bool) -> None:
    built = (NOW - age).isoformat()
    assert win.is_stale(built, NOW) is stale
    block = _block("M\tsrc/kiro_crew/plain.py\n", bundle={**BUNDLE, "built_at": built})
    assert (block["verdict"], str(block["reason"]).split(":")[0]) == (
        ("full", "bundle_stale") if stale else ("select", "None")
    )


@pytest.mark.parametrize(
    "built_at", ["left out", None, "", "yesterday", 1763640000, "2026-11-20T12:00:00", ["x"]]
)
def test_a_bundle_that_does_not_say_when_it_was_built_is_stale(built_at: object) -> None:
    bundle = {key: value for key, value in BUNDLE.items() if key != "built_at"}
    if built_at != "left out":
        bundle["built_at"] = built_at
    block = _block("M\tsrc/kiro_crew/plain.py\n", bundle=bundle)
    assert block["verdict"] == "full" and block["reason"].startswith("bundle_stale")


@pytest.mark.parametrize(
    "diff",
    [
        "A\tsrc/kiro_crew/new.py\n",
        "M\tdocs/guide.md\n",
        "M\tsrc/kiro_crew/unmapped.py\n",
        "M\ttest/conftest.py\n",
        "R100\tsrc/kiro_crew/plain.py\tsrc/kiro_crew/moved.py\n",
    ],
)
def test_a_full_linux_verdict_is_a_full_windows_verdict(diff: str) -> None:
    linux = _tiered(cts.ctm.parse_name_status(diff))
    assert linux["verdict"] == "full"
    assert _block(diff) == win.full(f"linux_full: {linux['reason']}")


def test_a_docs_only_diff_is_full_and_a_plain_src_diff_runs_guard_and_targeting_tests() -> None:
    docs = _block("M\tdocs/guide.md\nM\tREADME.md\n")
    assert docs["verdict"] == "full" and docs["reason"].startswith("linux_full: ")
    assert docs["run"] == [] and docs["would_run_share"] == 1.0
    # A `src/` file the table does not name, with an added line no pattern hits.
    path = "src/kiro_crew/plain.py"
    assert path not in BUNDLE["os_sensitive"] and cts.ost.tier_of("x = 1\n") is None
    plain = _block(f"M\t{path}\n", added={path: "x = 1\n"})
    assert plain["verdict"] == "select" and plain["reason"] is None
    reached = set(BUNDLE["windows_targeting"]) & _ad(f"M\t{path}\n")
    assert set(plain["run"]) == (set(GUARD) | reached) - IGNORED
    # The tests the map links to the file run on Linux; Windows leaves the others out.
    assert "test/test_plain.py" in _ad(f"M\t{path}\n") - set(plain["run"])


def _ci_bucket() -> list[str]:
    steps = load_workflow("ci.yml")["jobs"]["changes"]["steps"]
    filters = next(step["with"]["filters"] for step in steps if step.get("id") == "filter")
    return __import__("yaml").safe_load(filters)["windows"]


def test_the_bucket_is_read_from_ci_yml_as_yaml_reads_it(tmp_path: Path) -> None:
    assert win.path_bucket(ROOT) == _ci_bucket() != []
    workflow = tmp_path / win.CI_WORKFLOW
    workflow.parent.mkdir(parents=True)
    # A bucket this reader cannot take is an error, never an empty bucket.
    for text in ("jobs: {}\n", "  windows:\n  other:\n", "  windows:\n    - unquoted\n"):
        workflow.write_text(text, encoding="utf-8")
        with pytest.raises(ValueError):
            win.path_bucket(tmp_path)
    workflow.unlink()
    with pytest.raises(OSError):
        win.path_bucket(tmp_path)


def _path_for(glob: str) -> str:
    return glob.replace("**/", "deep/er/").replace("**", "any/file").replace("*", "name")


@pytest.mark.parametrize("glob", _ci_bucket())
def test_a_changed_path_in_the_bucket_is_a_full_windows_verdict(glob: str) -> None:
    path = _path_for(glob)
    assert win.glob_pattern(glob).fullmatch(path)
    # `**/` also matches no folder at all; `*` stays inside one path segment.
    assert win.glob_pattern(glob).fullmatch(path.replace("deep/er/", ""))
    if "*" in glob and "**" not in glob:
        assert not win.glob_pattern(glob).fullmatch(glob.replace("*", "na/me"))
    bucket = _ci_bucket()
    for diff in (f"M\t{path}\n", f"R100\t{path}\tsrc/kiro_crew/plain.py\n"):
        changes = cts.ctm.parse_name_status(diff)
        linux = {**_tiered([("M", ["src/kiro_crew/plain.py"])])}
        block = win.block(
            changes,
            linux,
            BUNDLE,
            sorted(TESTS),
            GUARD,
            added={},
            bucket=bucket,
            ignored=set(),
            at=NOW,
            score=_tiered,
        )
        assert block["verdict"] == "full" and block["reason"].startswith(f"windows_path: {path} ")


def test_the_share_weighs_the_files_windows_collects_by_their_node_counts() -> None:
    block = _block("M\tsrc/kiro_crew/lock.py\nM\ttest/test_other.py\n")
    collected = {name: nodes for name, nodes in TESTS.items() if name not in IGNORED}
    assert block["would_run_share"] == pytest.approx(
        sum(collected[name] for name in block["run"]) / sum(collected.values())
    )
    assert 0 < block["would_run_share"] < 1
    # A diff that reaches nothing on Windows still runs the guard tier.
    alone = _block("M\tsrc/kiro_crew/grown.py\n")
    assert alone["run"] == GUARD
    assert alone["would_run_share"] == pytest.approx(
        sum(collected[name] for name in GUARD) / sum(collected.values())
    )
    assert win.full("why")["would_run_share"] == 1.0


def _git(repo: Path, *argv: str, when: datetime | None = None) -> str:
    env = {**os.environ}
    if when is not None:
        env["GIT_COMMITTER_DATE"] = env["GIT_AUTHOR_DATE"] = when.isoformat()
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *argv],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=FALLBACK_TIMEOUT_SECS,
    ).stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tree with ci.yml's bucket, the ignore list, and two commits: `grown.py` gains a line."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "repo"
    _write(
        root,
        {
            win.CI_WORKFLOW: "filters: |\n  windows:\n    # by name\n    - '**/*windows*'\n  other:\n    - 'x'\n",
            cts.ost.COLLECT_IGNORE: "test_posix_only.py  # posix\n",
            "src/kiro_crew/grown.py": "x = 1\n",
            **{name: "" for name in TESTS},
            # Names the module by import alone: the map does not link it.
            "test/test_other.py": "import kiro_crew.grown\n",
        },
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base", when=NOW - timedelta(days=1))
    return root


@pytest.mark.parametrize("narrow_max", [0, 1, cts.NARROW_SEARCH_MAX])
def test_a_search_within_some_files_finds_what_the_whole_search_finds_there(
    repo: Path, monkeypatch: pytest.MonkeyPatch, narrow_max: int
) -> None:
    # Whether the files are searched alone or the whole tree is searched and filtered.
    monkeypatch.setattr(cts, "NARROW_SEARCH_MAX", narrow_max)
    modules = ["kiro_crew.grown"]
    whole = cts.git_grep(modules, repo)
    assert whole == {"test/test_other.py"}
    for within in (
        whole,
        {"test/test_other.py", "test/test_gate.py"},
        {"test/test_gate.py"},
        set(),
    ):
        assert cts.git_grep(modules, repo, within) == whole & within


def _score(repo: Path, tmp_path: Path, bundle: dict[str, Any], *argv: str) -> dict[str, Any]:
    (tmp_path / "bundle.json").write_text(json.dumps(bundle), encoding="utf-8")
    out = tmp_path / "verdict.json"
    args = ["--root", str(repo), "--bundle", str(tmp_path / "bundle.json"), "--out", str(out)]
    assert cts.main([*args, *argv]) == 0
    return json.loads(out.read_text(encoding="utf-8"))


def test_the_cli_writes_the_block_and_prints_its_reason_and_share(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Built a day inside the limit before the head was committed, and long before today.
    built = NOW - timedelta(days=win.STALE_DAYS - 1)
    assert win.is_stale(built.isoformat(), datetime.now(timezone.utc))
    bundle = {**BUNDLE, "guard": GUARD, "built_at": built.isoformat()}
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, {"src/kiro_crew/grown.py": "x = 1\nimport fcntl\n"})
    _git(repo, "commit", "-qam", "head", when=NOW)
    verdict = _score(repo, tmp_path, bundle, "--base", base, "--head", "HEAD")
    block = verdict["windows"]
    # The added line is read from the diff, the bucket and the ignore list from the tree.
    assert block["verdict"] == "select" and block["run"] == [
        "test/test_gate.py",
        "test/test_named.py",
        "test/test_other.py",
    ]
    line = capsys.readouterr().out.splitlines()[-1]
    assert line == (
        f"windows: select, would run 3 test files, run share {block['would_run_share']:.3f}, reason: None"
    )
    # Against the head's commit time, not the clock: the same bundle, built 8 days before.
    old = {**bundle, "built_at": (NOW - timedelta(days=win.STALE_DAYS + 1)).isoformat()}
    stale = _score(repo, tmp_path, old, "--base", base, "--head", "HEAD")["windows"]
    assert stale["verdict"] == "full" and stale["reason"].startswith("bundle_stale")
    assert f"reason: {stale['reason']}" in capsys.readouterr().out.splitlines()[-1]
    # A diff given as a file has no added lines and no head: every `.py` file counts, and
    # the bundle's age is taken against now.
    diff = tmp_path / "diff.txt"
    diff.write_text("M\tsrc/kiro_crew/plain.py\n", encoding="utf-8")
    fresh = {**bundle, "built_at": datetime.now(timezone.utc).isoformat()}
    by_file = _score(repo, tmp_path, fresh, "--name-status", str(diff))["windows"]
    assert set(by_file["run"]) == _ad("M\tsrc/kiro_crew/plain.py\n") - IGNORED
    assert _score(repo, tmp_path, old, "--name-status", str(diff))["windows"]["verdict"] == "full"


def test_the_guard_tier_of_the_block_is_the_one_the_diff_was_scored_with(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diff = tmp_path / "diff.txt"
    diff.write_text("M\tsrc/kiro_crew/plain.py\n", encoding="utf-8")
    fresh = {**BUNDLE, "built_at": datetime.now(timezone.utc).isoformat()}
    # A diff given as a file has no added lines, so the file's own tiers A to D run too.
    reached = _ad("M\tsrc/kiro_crew/plain.py\n") - set(GUARD)
    live = ["test/test_named.py", "test/test_posix_only.py"]
    monkeypatch.setattr(cts.ctm, "guard_tier", lambda root: live)
    # The bundle's own guard table, then the live scan for a bundle without one.
    for bundle, guard in (({**fresh, "guard": GUARD}, GUARD), (fresh, live)):
        block = _score(repo, tmp_path, bundle, "--name-status", str(diff))["windows"]
        assert block["verdict"] == "select"
        assert set(block["run"]) == (set(guard) | reached) - IGNORED
    # A head that adds a line no pattern hits, to a file the table does not name:
    # nothing but the live guard tier puts a test in the run.
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo, {"src/kiro_crew/grown.py": "x = 1\ny = 2\n"})
    _git(repo, "commit", "-qam", "head", when=datetime.now(timezone.utc))
    quiet = _score(repo, tmp_path, fresh, "--base", base, "--head", "HEAD")["windows"]
    assert quiet["verdict"] == "select" and set(quiet["run"]) == set(live) - IGNORED
    # No guard tier at all refuses to narrow on Linux, and so on Windows.
    monkeypatch.setattr(cts.ctm, "guard_tier", lambda root: [])
    none = _score(repo, tmp_path, fresh, "--name-status", str(diff))
    assert none["verdict"] == "full" and none["windows"]["reason"].startswith("linux_full: ")


def test_every_failure_is_a_full_windows_verdict(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = {**BUNDLE, "guard": GUARD, "built_at": datetime.now(timezone.utc).isoformat()}
    diff = tmp_path / "diff.txt"
    diff.write_text("M\tsrc/kiro_crew/plain.py\n", encoding="utf-8")
    argv = ("--name-status", str(diff))
    assert _score(repo, tmp_path, bundle, *argv)["windows"]["verdict"] == "select"
    # The scorer itself fails: both verdicts are full, for the same reason.
    failed = _score(repo, tmp_path, {"schema": 7}, *argv)
    assert failed["verdict"] == "full"
    assert failed["windows"] == win.full(f"linux_full: {failed['reason']}")
    # The tree cannot say what the bucket is: the Linux verdict stands, Windows runs everything.
    (repo / win.CI_WORKFLOW).write_text("jobs: {}\n", encoding="utf-8")
    unread = _score(repo, tmp_path, bundle, *argv)
    assert unread["verdict"] == "select"
    assert unread["windows"]["verdict"] == "full"
    assert unread["windows"]["reason"].startswith("error: ValueError")
    for verdict in (failed, unread):
        assert ledger.parse_windows(verdict["windows"]) is not None
