"""A data-home read the sandbox denies ends one doctor section, not the report.

Run from an agent's shell, ``kirocrew doctor`` sits inside the agent sandbox, which
hides part of the data home on purpose (the task store among it). A section whose
read is denied must report that one row and let every later section run, and an
unreadable data home seen from an unconfined shell must still count as an issue.
"""

from __future__ import annotations

import ast
import errno
import inspect
import textwrap
from pathlib import Path

import pytest

from kiro_crew import cli_doctor
from kiro_crew.doctor_checks import confinement, render


def _denied(path: str = "/data/op/.kiro/crew/tasks/tasks.db") -> PermissionError:
    return PermissionError(errno.EPERM, "Operation not permitted", path)


# ── the guard ─────────────────────────────────────────────────────────────────


def test_a_denied_read_inside_the_sandbox_is_a_skip_not_an_issue(capsys) -> None:
    issues: list[str] = []

    with render._unreadable_skips_section(issues, "task store", confined=lambda: True):
        raise _denied()

    out = capsys.readouterr().out
    assert out.startswith(
        "  task store: ⏭  skipped — '/data/op/.kiro/crew/tasks/tasks.db' is not "
        "readable from this shell\n"
    )
    assert "Run `kirocrew doctor` from your own" in out
    assert issues == []


def test_a_denied_read_outside_the_sandbox_is_an_issue(capsys) -> None:
    issues: list[str] = []

    with render._unreadable_skips_section(issues, "task store", confined=lambda: False):
        raise _denied()

    out = capsys.readouterr().out
    assert out.startswith(
        "  task store: ❌ '/data/op/.kiro/crew/tasks/tasks.db' is not readable "
        "(Operation not permitted)\n"
    )
    assert "check the ownership and permissions" in out
    assert issues == ["task store: '/data/op/.kiro/crew/tasks/tasks.db' is not readable"]


def test_a_confinement_probe_that_raises_keeps_the_stricter_verdict(capsys) -> None:
    def boom() -> bool:
        raise RuntimeError("probe broke")

    issues: list[str] = []

    with render._unreadable_skips_section(issues, "task store", confined=boom):
        raise _denied()

    assert "❌" in capsys.readouterr().out
    assert len(issues) == 1


def test_a_denial_with_no_filename_still_reports(capsys) -> None:
    issues: list[str] = []

    with render._unreadable_skips_section(issues, "task store", confined=lambda: True):
        raise PermissionError(errno.EACCES, "Permission denied")

    assert "skipped — a file it reads is not readable" in capsys.readouterr().out
    assert issues == []


def test_a_displayed_path_cannot_carry_terminal_controls(capsys) -> None:
    with render._unreadable_skips_section([], "task store", confined=lambda: True):
        raise _denied("/data/\x1b]0;spoof\x07/tasks.db")

    assert "\x1b" not in capsys.readouterr().out


@pytest.mark.parametrize("exc", [FileNotFoundError(2, "gone"), OSError(5, "EIO"), ValueError("x")])
def test_any_other_failure_still_propagates(exc) -> None:
    issues: list[str] = []

    with pytest.raises(type(exc)):
        with render._unreadable_skips_section(issues, "task store", confined=lambda: True):
            raise exc
    assert issues == []


def test_a_section_that_reads_fine_prints_only_its_own_rows(capsys) -> None:
    issues: list[str] = []

    with render._unreadable_skips_section(issues, "task store", confined=lambda: True):
        print("  task store: 2 queued")

    assert capsys.readouterr().out == "  task store: 2 queued\n"
    assert issues == []


# ── the task store, end to end ────────────────────────────────────────────────


def test_a_masked_task_store_no_longer_ends_the_report(tmp_path, monkeypatch, capsys) -> None:
    import kiro_crew.config.paths as paths

    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    (tmp_path / "tasks").mkdir()
    db = tmp_path / "tasks" / "tasks.db"
    real_exists = Path.exists

    def exists(self: Path, *args, **kwargs) -> bool:
        # What the macOS agent profile answers for a hidden leaf: EPERM on stat.
        if self == db:
            raise _denied(str(db))
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", exists)
    issues: list[str] = []

    with render._unreadable_skips_section(issues, "task store", confined=lambda: True):
        cli_doctor._doctor_task_store(issues)
    print("  next section ran")

    out = capsys.readouterr().out
    assert f"task store: ⏭  skipped — {str(db)!r} is not readable" in out
    assert out.endswith("  next section ran\n")
    assert issues == []


def test_doctor_runs_the_task_store_under_the_guard() -> None:
    """``_doctor`` must call the task-store row inside the guard, with the vantage probe."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(cli_doctor._doctor)))
    guarded: list[ast.With] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        for item in node.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "_unreadable_skips_section"
            ):
                guarded.append(node)
    assert guarded, "the task-store row is not wrapped"
    calls = {
        sub.func.attr
        for node in guarded
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
    }
    assert "_doctor_task_store" in calls
    keywords = {
        kw.arg: ast.unparse(kw.value)
        for node in guarded
        for item in node.items
        for kw in item.context_expr.keywords  # type: ignore[attr-defined]
    }
    assert keywords.get("confined") == "confinement._doctor_vantage_confined"


# ── the vantage probe ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("evidence", "userns", "expected"),
    [
        ("the kernel reports this process is Seatbelt-confined", None, True),
        (None, True, True),
        (None, False, False),
        (None, None, False),
    ],
)
def test_the_vantage_probe(monkeypatch, evidence, userns, expected) -> None:
    monkeypatch.setattr(cli_doctor.sandbox, "agent_confinement_evidence", lambda: evidence)
    monkeypatch.setattr(cli_doctor, "_process_userns_vantage_confined", lambda: userns)

    assert confinement._doctor_vantage_confined() is expected
