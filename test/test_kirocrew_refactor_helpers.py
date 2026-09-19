"""Contract tests for the bundled Kiro Crew refactor evidence helpers."""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "kirocrew-codebase-refactor"
    / "scripts"
)
SCAN_PATH = SCRIPT_DIR / "scan_refactor_hotspots.py"
AUDIT_PATH = SCRIPT_DIR / "audit_refactor_overlap.py"
SKILL_PATH = SCRIPT_DIR.parent / "SKILL.md"


def _load(name: str, path: Path) -> ModuleType:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPT_DIR))


SCAN = _load("kirocrew_refactor_scan", SCAN_PATH)
AUDIT = _load("kirocrew_refactor_audit", AUDIT_PATH)
LIMITS = _load("kirocrew_refactor_limits", SCRIPT_DIR / "refactor_limits.py")


def test_skill_helper_commands_use_the_injected_runtime_python() -> None:
    skill_docs = [SKILL_PATH, *sorted((SKILL_PATH.parent / "references").glob("*.md"))]
    bare_helper = re.compile(r"(?m)^python3?\s+[^\n]*\$SKILL_DIR/scripts/")

    for path in skill_docs:
        assert not bare_helper.search(path.read_text(encoding="utf-8")), path

    skill = SKILL_PATH.read_text(encoding="utf-8")

    assert skill.count('"$KIROCREW_RUNTIME_PYTHON" "$SKILL_DIR/scripts/') == 2
    assert skill.count('& $env:KIROCREW_RUNTIME_PYTHON "$SKILL_DIR/scripts/') == 2


def test_path_history_accounts_for_truncated_git_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(AUDIT, "git", lambda *_args, **_kwargs: "1\n")
    monkeypatch.setattr(
        AUDIT,
        "git_lines",
        lambda *_args, **_kwargs: (["a" * 40 + "\t2026-01-01T00:00:00Z\tpartial"], 1),
    )

    history, overflow = AUDIT.path_history(Path("."), "base", "upstream", "src")

    assert overflow == 1
    assert history["latest"]["subject"] == "partial"
    assert history["latest"]["subject_truncated"] is True


def _run_scan(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCAN_PATH), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_missing_root_fails_closed_without_a_traceback(tmp_path: Path) -> None:
    result = _run_scan("--root", str(tmp_path / "missing"), "--format", "json")

    assert result.returncode == 2
    assert "source root is not a directory" in result.stderr
    assert "traceback" not in result.stderr.lower()


def test_inventory_failure_is_a_controlled_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_inventory(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("git ls-files failed (128)")

    monkeypatch.setattr(SCAN, "tracked_files", fail_inventory)
    monkeypatch.setattr(sys, "argv", [str(SCAN_PATH), "--root", str(tmp_path)])

    assert SCAN.main() == 2
    captured = capsys.readouterr()
    assert "source inventory failed: git ls-files failed (128)" in captured.err
    assert "traceback" not in captured.err.lower()


def test_invalid_thresholds_are_an_argparse_error() -> None:
    result = _run_scan("--thresholds", "3000,not-a-number")

    assert result.returncode == 2
    assert "thresholds must be comma-separated integers" in result.stderr
    assert "traceback" not in result.stderr.lower()


def test_markdown_rendering_neutralizes_filename_controls() -> None:
    unsafe = "bad\n\x1b[31m|`name.py"
    report = {
        "collected_at": "now",
        "root": ".",
        "git": {"head": "abc", "dirty": False},
        "totals": {"files": 1, "physical_lines": 1, "nonblank_lines": 1},
        "retention_overflow": {"files": 0, "paths": 0, "warnings": 0},
        "threshold_counts": {"3000": 0},
        "files": [{"path": unsafe, "physical_lines": 1, "nonblank_lines": 1}],
        "python": {"files": 0},
        "warnings": [unsafe],
    }

    rendered = SCAN.render_markdown(report, 1)

    assert "\x1b" not in rendered
    assert "bad\n" not in rendered
    assert "\\u000a" in rendered
    assert "\\u001b" in rendered
    assert "\\u007c" in rendered
    assert "\\u0060" in rendered


def test_git_path_inventory_uses_nul_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: tuple[str, ...] = ()

    def fake_fields(_root: Path, *args: str, check: bool = True) -> tuple[list[str], int]:
        nonlocal observed
        observed = args
        assert check
        return ["src/new\nline.py", "src/tab\tname.py"], 0

    monkeypatch.setattr(AUDIT, "git_nul_fields", fake_fields)

    paths, overflow = AUDIT.changed_files(Path("."), "left", "right")

    assert "-z" in observed
    assert "--no-renames" in observed
    assert paths == {"src/new\nline.py", "src/tab\tname.py"}
    assert overflow == 0


def test_overlap_audit_uses_shared_divergence_contract() -> None:
    args = AUDIT.divergence_count_args("upstream-sha", head="scope-head-sha")
    counts = AUDIT.parse_divergence_counts("4\t7\n")

    assert args[-1] == "scope-head-sha...upstream-sha"
    assert counts is not None
    assert counts.ahead == 4
    assert counts.behind == 7


def test_git_nul_fields_does_not_overflow_at_exact_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        stdout = io.BytesIO(b"one.py\0two.py\0")

        @staticmethod
        def wait() -> int:
            return 0

    monkeypatch.setattr(AUDIT, "MAX_RETAINED_ITEMS", 2)
    monkeypatch.setattr(AUDIT.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    fields, overflow = AUDIT.git_nul_fields(Path("."), "diff", "-z")

    assert fields == ["one.py", "two.py"]
    assert overflow == 0


def test_cli_collections_and_values_are_bounded() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--path",
        action=LIMITS.BoundedAppendAction,
        type=LIMITS.bounded_cli_text,
        default=[],
    )
    too_many = [
        item for index in range(LIMITS.MAX_CLI_ITEMS + 1) for item in ("--path", str(index))
    ]

    with pytest.raises(SystemExit):
        parser.parse_args(too_many)
    with pytest.raises(argparse.ArgumentTypeError):
        LIMITS.bounded_cli_text("x" * (LIMITS.MAX_RETAINED_STRING_CHARS + 1))
    with pytest.raises(argparse.ArgumentTypeError):
        SCAN.parse_thresholds(",".join(str(index + 1) for index in range(LIMITS.MAX_CLI_ITEMS + 1)))


def test_non_utf8_git_path_bytes_remain_json_serializable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        stdout = io.BytesIO(b"bad\xff.py\0")

        @staticmethod
        def wait() -> int:
            return 0

    monkeypatch.setattr(AUDIT.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())

    fields, overflow = AUDIT.git_nul_fields(Path("."), "diff", "-z")
    rendered = json.dumps({"paths": fields}, ensure_ascii=True)

    assert overflow == 0
    assert fields == ["bad\udcff.py"]
    assert "\\udcff" in rendered


def test_tracked_git_inventory_deduplicates_unmerged_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    class FakeProcess:
        stdout = io.BytesIO(b"same.py\0")

        @staticmethod
        def wait() -> int:
            return 0

    def fake_popen(args: list[str], **_kwargs: object) -> FakeProcess:
        observed.extend(args)
        return FakeProcess()

    monkeypatch.setattr(SCAN.subprocess, "Popen", fake_popen)

    paths = list(SCAN.tracked_files(Path("."), True, {"paths": 0}))

    assert paths == [Path("same.py")]
    assert "--deduplicate" in observed


def test_nested_root_intersects_explicit_prefix(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    wanted = repo / "src" / "kiro_crew" / "acp"
    outside = repo / "src" / "kiro_crew" / "other"
    wanted.mkdir(parents=True)
    outside.mkdir(parents=True)
    (wanted / "keep.py").write_text("x = 1\n", encoding="utf-8")
    (outside / "drop.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")

    result = _run_scan(
        "--root",
        str(repo / "src" / "kiro_crew"),
        "--path-prefix",
        "src/kiro_crew/acp",
        "--format",
        "json",
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert [item["path"] for item in report["files"]] == ["src/kiro_crew/acp/keep.py"]
    assert report["scope"]["requested_root_prefix"] == "src/kiro_crew"


def test_root_dot_matches_every_tracked_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "one.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")

    result = _run_scan("--root", str(repo), "--path-prefix", ".", "--format", "json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["totals"]["files"] == 1


def test_source_reads_and_retained_strings_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "large.py"
    source.write_bytes(b"12345")
    monkeypatch.setattr(SCAN, "MAX_SOURCE_FILE_BYTES", 4)

    item, warning = SCAN.inspect_file(tmp_path, Path("large.py"))
    bounded, truncated = LIMITS.bounded_text("x" * (LIMITS.MAX_RETAINED_STRING_CHARS + 1))

    assert item is None
    assert "above the 4-byte cap" in str(warning)
    assert truncated
    assert len(bounded) == LIMITS.MAX_RETAINED_STRING_CHARS


def test_source_reads_use_descriptor_pinned_root_confinement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, object] = {}

    def guarded_read(
        raw: str, within_root: str | None = None, *, max_bytes: int | None = None
    ) -> bytes:
        observed.update(raw=raw, within_root=within_root, max_bytes=max_bytes)
        return b"x = 1\n"

    monkeypatch.setattr(SCAN, "safe_read_file_bytes_nolink", guarded_read)

    item, warning = SCAN.inspect_file(tmp_path, Path("module.py"))

    assert warning is None
    assert item is not None
    assert observed == {
        "raw": str(tmp_path / "module.py"),
        "within_root": str(tmp_path),
        "max_bytes": SCAN.MAX_SOURCE_FILE_BYTES,
    }
