"""``kirocrew bench`` CLI behaviour that a reviewer flagged and a test must pin.

Two of these lock in fixes for GPT-review findings on PR #2123, and the third
pins the deliberate exception to the ``top-level-imports`` rule so a future
"cleanup" cannot silently regress the boot path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.cli_bench import _load_report, bench_cmd
from kiro_crew.eval.bench.run import compare_reports


class _Args:
    """A stand-in for the argparse namespace the dispatch receives."""

    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


# ── The sensitive-path gate on `bench compare` ───────────────────────────────
# These paths come from argv, and in this product argv is not always typed by the
# human who owns the machine: an agent can run any CLI command, so
# `kirocrew bench compare ~/.aws/credentials x.json` is a reachable invocation.
# Without the gate this subcommand is a file-read primitive that bypasses the
# check every other read path in the codebase goes through.


def test_a_sensitive_path_is_refused_rather_than_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # is_sensitive_path anchors on the REAL home, so a fake .aws under tmp_path is
    # NOT sensitive and the assertion would pass for the wrong reason (it would
    # fall through to the JSON branch). Re-anchor home at the fixture so the
    # production gate is genuinely exercised.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    aws = tmp_path / ".aws"
    aws.mkdir()
    secret = aws / "credentials"
    secret.write_text("[default]\naws_secret_access_key = SHOULD-NEVER-BE-PRINTED\n")

    with pytest.raises(Exception) as exc:  # _BenchError, caught by the dispatch
        _load_report(str(secret), "baseline")
    assert getattr(exc.value, "code", None) == 1
    out = capsys.readouterr().out
    assert "sensitive location" in out.lower()
    # The refusal must not leak the very bytes it refused to serve...
    assert "SHOULD-NEVER-BE-PRINTED" not in out
    # ...nor echo the path back. Resolution follows symlinks, so printing the
    # resolved form would disclose more than the caller supplied, and an
    # argv-derived string reaching stdout is a taint flow a scanner cannot tell
    # apart from a real leak (CodeQL py/clear-text-logging-sensitive-data).
    assert str(secret) not in out
    assert ".aws" not in out


@requires_symlinks
def test_a_symlink_into_a_sensitive_path_is_refused_through_the_link(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate resolves before checking, so a link cannot launder the target."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text("[default]\n")
    link = tmp_path / "innocent-report.json"
    link.symlink_to(aws / "credentials")

    with pytest.raises(Exception) as exc:
        _load_report(str(link), "baseline")
    assert getattr(exc.value, "code", None) == 1
    out = capsys.readouterr().out
    assert "sensitive location" in out.lower()
    assert str(link) not in out


def test_a_missing_report_prints_one_line_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(Exception) as exc:
        _load_report(str(tmp_path / "nope.json"), "candidate")
    assert getattr(exc.value, "code", None) == 1
    out = capsys.readouterr().out
    assert "candidate report not found" in out
    assert str(tmp_path) not in out  # the path must not be echoed
    assert "Traceback" not in out


def test_a_truncated_report_names_the_likely_cause(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An interrupted run is the usual source, so the message should say so."""
    bad = tmp_path / "half.json"
    bad.write_text('{"corpus": {"name": "locomo"')  # truncated mid-object
    with pytest.raises(Exception) as exc:
        _load_report(str(bad), "baseline")
    assert getattr(exc.value, "code", None) == 1
    out = capsys.readouterr().out
    assert "not valid JSON" in out
    assert "interrupted run" in out


def test_a_json_scalar_is_rejected_as_not_a_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Valid JSON is not the same as a report; a bare list would KeyError later."""
    bad = tmp_path / "list.json"
    bad.write_text("[1, 2, 3]")
    with pytest.raises(Exception) as exc:
        _load_report(str(bad), "baseline")
    assert getattr(exc.value, "code", None) == 1
    assert "not a report object" in capsys.readouterr().out


def test_compare_returns_one_on_a_bad_path_and_zero_on_two_good_ones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    good = {
        "corpus": {"fingerprint": "abc"},
        "config": {"ingest": {}, "retrieval": {}, "search_backend": "sqlite_cosine"},
        "metrics": {"session": {"recall_all@5": 0.5}},
    }
    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps(good))
    b.write_text(json.dumps(good))

    assert bench_cmd(_Args(bench_action="compare", baseline=str(a), candidate=str(b), k=5)) == 0
    capsys.readouterr()
    assert (
        bench_cmd(
            _Args(bench_action="compare", baseline=str(tmp_path / "gone.json"),
                  candidate=str(b), k=5)
        )
        == 1
    )


# ── An incompatible pair must not publish a delta ────────────────────────────


def _report(fingerprint: str, *, backend: str = "sqlite_cosine", recall: float = 0.5) -> dict:
    return {
        "corpus": {"fingerprint": fingerprint},
        "config": {"ingest": {"granularity": "turn"}, "retrieval": {"mmr": True},
                   "search_backend": backend},
        "metrics": {
            "session": {"recall_all@5": recall, "ndcg@5": recall},
            # Matching populations: without these the comparison is refused, which is
            # the point of the guard rather than a fixture wart.
            "session_measurable": {"5": 1977},
        },
    }


@pytest.mark.parametrize(
    "candidate,reason",
    [
        (_report("different"), "fingerprint"),
        (_report("abc", backend="faiss"), "search_backend"),
    ],
)
def test_incompatible_reports_report_no_delta_at_all(candidate: dict, reason: str) -> None:
    """Printing an "incompatible" banner and then exact-looking deltas invites
    exactly the reading the banner exists to prevent.

    The closing note on the comparable path asserts the deltas ARE exact, which is
    false once the inputs differ — so the table must not be reached, not merely be
    preceded by a warning.
    """
    out = compare_reports(_report("abc", recall=0.5), candidate, k=5)
    assert "## Not comparable" in out
    assert reason in out
    assert "No delta is reported" in out
    # No metric table and no exactness claim. Checked structurally rather than by
    # keyword: the refusal text itself legitimately says "No delta is reported".
    assert "recall_all@5" not in out
    assert "| baseline |" not in out
    assert "exact" not in out.lower()


def test_a_comparable_pair_still_reports_the_delta_and_its_exactness() -> None:
    """The guard must not have made the happy path silent too."""
    out = compare_reports(_report("abc", recall=0.4), _report("abc", recall=0.6), k=5)
    assert "## Not comparable" not in out
    assert "recall_all@5" in out
    assert "+0.2000" in out
    assert "deterministic" in out


# ── The deliberate lazy import (AUTOSDE top-level-imports exception) ─────────


def test_importing_cli_bench_does_not_drag_the_vector_store_into_the_boot_path() -> None:
    """``cli.py`` imports ``cli_bench`` at module scope, so this is every command's cost.

    Measured on the authoring host: hoisting the bench imports to module scope
    takes the import from 152 to 432 modules and 0.046s to 0.254s, and pulls
    ``vector_memory`` + ``sqlite3`` into commands that will never touch a
    benchmark. This is the same class of regression ``test_perf_boot_path.py``
    pins, and it is why the function-local imports in ``cli_bench`` are a
    deliberate exception to the (non-blocking) ``top-level-imports`` rule rather
    than an oversight — a future cleanup that hoists them fails here.

    A fresh interpreter is required: pytest has already imported most of the
    package, so ``sys.modules`` in-process says nothing about what one import pulls.
    """
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = textwrap.dedent(
        """
        import json, sys
        import kiro_crew.cli_bench  # noqa: F401
        print(json.dumps({
            "vector_memory": "kiro_crew.vector_memory" in sys.modules,
            "bench": "kiro_crew.eval.bench" in sys.modules,
            "sqlite3": "sqlite3" in sys.modules,
        }))
        """
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    # Coverage's subprocess hook imports extra modules and would pollute the
    # module-presence assertions below.
    env.pop("COV_CORE_SOURCE", None)
    env.pop("COVERAGE_PROCESS_START", None)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    loaded = json.loads(proc.stdout)
    assert loaded["vector_memory"] is False, "cli_bench must not import the vector store"
    assert loaded["bench"] is False, "cli_bench must not import the bench package eagerly"
    assert loaded["sqlite3"] is False, "cli_bench must not pull sqlite3 into every command"
