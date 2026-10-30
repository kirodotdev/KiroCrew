"""``scripts/net_strict_changed.py`` runs the test files a change touches with the network floor strict.

The rootdir floor already refuses an off-loopback connect, send or name lookup under
``KIROCREW_NET_STRICT=1`` (raising at the call, failing the test at teardown) and CI
never sets it. The script sets it for the changed test files only, so a file is held
to testing-conventions D10 from the day someone touches it and no baseline needs
cleaning. These tests pin the three parts: which files it picks, that a changed file
with a non-local name lookup is reported while an unchanged one is not, and how the
report reads.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from source_corpus import repo_root
from test_ratchet_scope import (
    _GIT_COMMAND_LINE_CONFIG,
    _fixture_git_env,
    assert_fixture_ignores_ambient_git,
)

from kiro_crew.platform.update_governance import _GIT_LOCATION_VARS

ROOT = repo_root()
_SPEC = importlib.util.spec_from_file_location(
    "net_strict_changed", ROOT / "scripts" / "net_strict_changed.py"
)
assert _SPEC and _SPEC.loader
strict = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(strict)

#: The probe fixtures never touch the network. ``sys.audit`` raises the same event
#: ``socket.getaddrinfo`` raises, which is all the floor's hook reads, so the floor
#: judges it exactly as it would a real lookup while nothing leaves this host even
#: if the child fails to load the floor.
#:
#: This one looks up a name nobody on the runner owns, and swallows the refusal like a
#: test that wraps its call in ``except OSError`` would.
_LOOKUP = """import sys


def test_looks_up_a_remote_name():
    try:
        sys.audit("socket.getaddrinfo", "strict-probe.example.invalid", 443, 0, 0, 0)
    except OSError:
        pass
"""

_CLEAN = """import sys


def test_stays_on_loopback():
    sys.audit("socket.getaddrinfo", "localhost", 80, 0, 0, 0)
    sys.audit("socket.getaddrinfo", "127.0.0.1", 80, 0, 0, 0)
"""

_MARKED = """import sys

import pytest


@pytest.mark.real_network
def test_reaches_out_on_purpose():
    try:
        sys.audit("socket.getaddrinfo", "strict-probe.example.invalid", 443, 0, 0, 0)
    except OSError:
        pass
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        env=_fixture_git_env(),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """``main`` holds four test files; the checkout is a branch off it with no change yet."""
    for name in (*_GIT_LOCATION_VARS, *_GIT_COMMAND_LINE_CONFIG):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GITHUB_BASE_REF", "GITHUB_EVENT_NAME", "FLAKE_RATCHET_BASE"):
        monkeypatch.delenv(name, raising=False)
    root = tmp_path / "repo"
    (root / "test").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "test" / "test_changed_probe.py").write_text(_LOOKUP, encoding="utf-8")
    (root / "test" / "test_unchanged_probe.py").write_text(_LOOKUP, encoding="utf-8")
    (root / "test" / "test_marked_probe.py").write_text(_MARKED, encoding="utf-8")
    (root / "test" / "test_deleted_probe.py").write_text(_LOOKUP, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "checkout", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "change")
    return root


def _commit_all(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "change")


def test_the_git_fixture_ignores_ambient_git_state(request, monkeypatch, tmp_path) -> None:
    assert_fixture_ignores_ambient_git(request, monkeypatch, tmp_path, "repo")


# ── which files are picked ────────────────────────────────────────────


@pytest.mark.parametrize(
    "rel, expected",
    [
        ("test/test_a.py", True),
        ("test/sub/test_a.py", True),
        ("test/a_test.py", True),
        ("src/kiro_crew/apps/builtins/x/tests/test_a.py", True),
        ("test/conftest.py", False),
        ("test/helpers.py", False),
        ("test/test_a.txt", False),
        ("src/kiro_crew/session.py", False),
        ("scripts/test_a.py", False),
        ("-rf/test_a.py", False),
        ("test/test_a.py::test_b", False),
        ("test/test_a\nb.py", False),
        ("test/test_a\x1b.py", False),
    ],
)
def test_only_a_collectable_test_module_with_a_plain_name_is_run(rel, expected) -> None:
    assert strict.is_runnable_name(rel) is expected


def test_selection_skips_deletions_missing_files_and_non_tests(tmp_path) -> None:
    for rel in ("test/test_kept.py", "test/test_moved.py", "test/helper.py"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("", encoding="utf-8")
    pairs = [
        ("test/test_kept.py", "test/test_kept.py"),
        (None, "test/test_kept.py"),  # listed twice: run once
        ("test/test_old_name.py", "test/test_moved.py"),  # a rename is its destination
        ("test/test_gone.py", None),  # deleted
        (None, "test/test_not_on_disk.py"),
        (None, "test/helper.py"),
    ]
    assert strict.select_test_files(pairs, tmp_path) == ["test/test_kept.py", "test/test_moved.py"]
    assert strict.select_test_files([], tmp_path) == []


def test_the_file_list_is_what_the_change_adds_or_changes(repo) -> None:
    (repo / "test" / "test_changed_probe.py").write_text(_LOOKUP + "\n# edit\n", encoding="utf-8")
    (repo / "test" / "test_added_probe.py").write_text(_CLEAN, encoding="utf-8")
    (repo / "test" / "conftest.py").write_text("", encoding="utf-8")
    (repo / "notes.txt").write_text("x\n", encoding="utf-8")
    (repo / "test" / "test_deleted_probe.py").unlink()
    _commit_all(repo)
    files, how = strict.changed_test_files(repo)
    assert files == ["test/test_added_probe.py", "test/test_changed_probe.py"]
    assert "main" in how


def test_a_change_with_no_test_file_selects_nothing(repo) -> None:
    (repo / "notes.txt").write_text("x\n", encoding="utf-8")
    _commit_all(repo)
    assert strict.changed_test_files(repo)[0] == []


def test_an_unreadable_base_is_none_not_an_empty_list(repo, monkeypatch) -> None:
    monkeypatch.setenv("FLAKE_RATCHET_BASE", "no-such-ref")
    files, how = strict.changed_test_files(repo)
    assert files is None and "no-such-ref" in how


def test_main_skips_without_running_pytest_when_nothing_changed(monkeypatch, capsys) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("pytest must not start when no test file changed")

    monkeypatch.setattr(strict, "changed_test_files", lambda _root=ROOT: ([], "the test base"))
    monkeypatch.setattr(strict, "run_strict", refuse)
    assert strict.main() == 0
    assert "skipped" in capsys.readouterr().out
    monkeypatch.setattr(strict, "changed_test_files", lambda _root=ROOT: (None, "no base"))
    assert strict.main() == 0
    assert "nothing checked" in capsys.readouterr().out


# ── a changed file is judged, an unchanged one is not ─────────────────


def _strict_run(repo: Path, files: list[str], monkeypatch, tmp_path: Path):
    """Run the changed ``files`` the way the script does, inside the scratch repo.

    The scratch repo has no conftest of its own, so the real rootdir conftest (which
    holds the floor) is loaded by name from the checkout under test.
    """
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")]))
    # This test's own data-home pin is not the child's: the child makes its own floor.
    monkeypatch.delenv("KIROCREW_HOME", raising=False)
    junit = tmp_path / "junit.xml"
    finished, _code = strict.run_strict(
        files, root=repo, junit=junit, extra_args=("-p", "conftest"), timeout=240
    )
    assert finished
    return strict.parse_report(junit)


def test_a_changed_file_with_a_remote_name_lookup_is_reported_and_unchanged_ones_are_not(
    repo, monkeypatch, tmp_path
) -> None:
    (repo / "test" / "test_changed_probe.py").write_text(_LOOKUP + "\n# edit\n", encoding="utf-8")
    _commit_all(repo)
    files, _how = strict.changed_test_files(repo)
    assert files == ["test/test_changed_probe.py"]
    findings, others = _strict_run(repo, files, monkeypatch, tmp_path)
    assert [(test_id.split("::")[-1], detail) for test_id, detail in findings] == [
        ("test_looks_up_a_remote_name", "name lookup 'strict-probe.example.invalid'")
    ]
    assert others == 0
    # The unchanged file holds the same lookup and is never run, so it is never named.
    assert all("unchanged" not in test_id for test_id, _detail in findings)


def test_loopback_and_a_real_network_marker_are_not_reported(repo, monkeypatch, tmp_path) -> None:
    (repo / "test" / "test_clean_probe.py").write_text(_CLEAN, encoding="utf-8")
    (repo / "test" / "test_marked_probe.py").write_text(_MARKED + "\n# edit\n", encoding="utf-8")
    _commit_all(repo)
    files, _how = strict.changed_test_files(repo)
    assert files == ["test/test_clean_probe.py", "test/test_marked_probe.py"]
    assert _strict_run(repo, files, monkeypatch, tmp_path) == ([], 0)


# ── the report ────────────────────────────────────────────────────────


def _junit(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "j.xml"
    path.write_text(
        f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>', "utf-8"
    )
    return path


def test_both_strict_failure_spellings_are_one_finding_and_other_failures_are_counted(
    tmp_path,
) -> None:
    body = (
        '<testcase classname="test.test_a" name="test_x">'
        '<failure message="OffLoopbackNetworkRefused: off-loopback network refused in a test: '
        'connect 203.0.113.9:443">trace</failure>'
        '<error message="failed on teardown with AssertionError: this test reached past '
        "loopback under KIROCREW_NET_STRICT=1: connect 203.0.113.9:443. Stub the network at "
        'the seam">trace</error></testcase>'
        '<testcase classname="test.test_a" name="test_y"><failure message="assert 1 == 2">t'
        "</failure></testcase>"
        '<testcase classname="test.test_a" name="test_z"/>'
    )
    findings, others = strict.parse_report(_junit(tmp_path, body))
    assert findings == [("test.test_a::test_x", "connect 203.0.113.9:443")]
    assert others == 1


def test_a_missing_or_broken_report_is_none_not_clean(tmp_path) -> None:
    assert strict.parse_report(tmp_path / "absent.xml") is None
    broken = tmp_path / "broken.xml"
    broken.write_text("<testsuites", encoding="utf-8")
    assert strict.parse_report(broken) is None


def test_a_report_that_declares_an_entity_is_refused_not_expanded(tmp_path) -> None:
    # The changed test files run in the child, so they can write this file themselves.
    hostile = tmp_path / "hostile.xml"
    hostile.write_text(
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "boom">]><testsuites>&x;</testsuites>',
        encoding="utf-8",
    )
    assert strict.parse_report(hostile) is None


def test_the_report_names_the_finding_and_cannot_forge_a_log_line() -> None:
    lines = strict.render(
        ["test/test_a.py"],
        ([("t::x", "name lookup '\n::error::forged `tick`'")], 2),
        finished=True,
        code=1,
        how="the merge-base with main",
    )
    text = "\n".join(lines)
    assert "name lookup" in text and "2 test(s) failed for another reason" in text
    assert not any(line.startswith("::") for line in lines)
    assert "`tick`" not in text
    clean = strict.render(["test/test_a.py"], ([], 0), finished=True, code=0, how="main")
    assert "No test reached past loopback." in "\n".join(clean)


def test_a_run_that_measured_nothing_never_says_it_was_clean() -> None:
    for finished, code, words in ((False, -1, "did not finish"), (True, 2, "exited with code 2")):
        text = "\n".join(
            strict.render(["test/test_a.py"], None, finished=finished, code=code, how="m")
        )
        assert words in text and "Nothing was measured" in text
        assert "No test reached past loopback" not in text


def test_main_writes_the_report_file_and_never_the_step_summary(monkeypatch, tmp_path) -> None:
    summary = tmp_path / "step-summary.md"
    report = tmp_path / "report.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv(strict.REPORT_ENV, str(report))
    monkeypatch.setattr(strict, "changed_test_files", lambda _root=ROOT: (["test/test_a.py"], "b"))
    monkeypatch.setattr(strict, "run_strict", lambda *_a, **_k: (True, 1))
    monkeypatch.setattr(strict, "parse_report", lambda _path: ([("t::x", "name lookup 'h'")], 0))
    assert strict.main() == 1
    assert "name lookup 'h'" in report.read_text(encoding="utf-8")
    assert not summary.exists(), "an unprivileged step cannot write the step summary on CodeBuild"
    monkeypatch.setattr(strict, "parse_report", lambda _path: ([], 0))
    assert strict.main() == 0
    assert "No test reached past loopback." in report.read_text(encoding="utf-8")
    monkeypatch.setattr(strict, "run_strict", lambda *_a, **_k: (False, -1))
    monkeypatch.setattr(strict, "parse_report", lambda _path: None)
    assert strict.main() == 1
    assert "Nothing was measured" in report.read_text(encoding="utf-8")


def test_main_reports_a_skip_in_the_file_too(monkeypatch, tmp_path) -> None:
    report = tmp_path / "report.md"
    monkeypatch.setenv(strict.REPORT_ENV, str(report))
    monkeypatch.setattr(strict, "changed_test_files", lambda _root=ROOT: ([], "b"))
    assert strict.main() == 0
    assert "adds or changes no test file" in report.read_text(encoding="utf-8")


def test_an_unwritable_report_path_does_not_crash_the_step(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv(strict.REPORT_ENV, str(tmp_path / "missing-dir" / "report.md"))
    monkeypatch.setattr(strict, "changed_test_files", lambda _root=ROOT: ([], "b"))
    assert strict.main() == 0
    assert "could not write the report file" in capsys.readouterr().out


# ── the step that publishes the report runs as the runner identity ────


def _publish_step() -> dict:
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "fast-gate.yml").read_text("utf-8"))
    steps = workflow["jobs"]["flake-pattern-ratchet"]["steps"]
    (step,) = [s for s in steps if s.get("name", "").startswith("Publish the network report")]
    return step


def test_the_publish_step_starts_python_without_site_or_the_working_directory() -> None:
    step = _publish_step()
    assert step["run"].lstrip().startswith("python3 -I -S -"), step["run"][:60]
    assert "shell" not in step, "the publish step must not drop to the unprivileged ci-shell"
    assert step["continue-on-error"] is True and step["if"] == "always()"


@pytest.mark.skipif(
    sys.platform == "win32" or shutil.which("bash") is None or shutil.which("python3") is None,
    reason="the publish step is a Linux-only bash step in a Linux-only job",
)
def test_the_publish_step_ignores_a_sitecustomize_the_tests_left_behind(tmp_path) -> None:
    run = _publish_step()["run"]
    temp, work = tmp_path / "runner-temp", tmp_path / "workspace"
    temp.mkdir()
    work.mkdir()
    marker = tmp_path / "ran-as-root"
    # What a changed test could drop into the checkout: the editable install puts the
    # workspace's src/ on the site path and a shell step starts in the workspace.
    (work / "sitecustomize.py").write_text(f"open({str(marker)!r}, 'w').close()\n", "utf-8")
    (temp / "net-strict-report.md").write_text("found nothing\n", encoding="utf-8")
    summary = tmp_path / "summary.md"

    def publish(script: str) -> str:
        summary.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            "RUNNER_TEMP": str(temp),
            "GITHUB_STEP_SUMMARY": str(summary),
            "PYTHONPATH": str(work),
        }
        subprocess.run(["bash", "-e", "-c", script], cwd=work, env=env, check=True, timeout=60)
        return summary.read_text(encoding="utf-8")

    unsafe = run.replace("python3 -I -S -", "python3 -", 1)
    assert publish(unsafe) == "found nothing\n"
    assert marker.exists(), "control: a bare python3 must run the planted module"
    marker.unlink()
    assert publish(run) == "found nothing\n"
    assert not marker.exists(), "the planted sitecustomize.py ran in the publish step"
    # A link, or a file that is not regular, publishes nothing.
    (temp / "net-strict-report.md").unlink()
    (temp / "net-strict-report.md").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text("do not publish\n", encoding="utf-8")
    assert publish(run) == ""
