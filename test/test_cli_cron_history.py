"""``kirocrew cron history``: the read-only CLI over the cron run store.

Runs are seeded through ``CronHistoryStore.append`` with fixed timestamps and
read back through the real ``kirocrew cron`` parser and dispatcher.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from unittest.mock import patch

import pytest

from kiro_crew.cli_commands import _cron
from kiro_crew.cli_parser_agent import register_cron_parser
from kiro_crew.config.loader import config_dir
from kiro_crew.cron import CronService
from kiro_crew.cron_history import CronHistoryStore, CronRunRecord

SECRET = "AKIAIOSFODNN7EXAMPLE"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    monkeypatch.setenv("KIROCREW_HOME", str(root))
    # Not the runner's own confinement: an agent shell running pytest has it.
    monkeypatch.setattr("kiro_crew.cli_cron_history._sandbox_confined", lambda: False)
    config_dir().mkdir(parents=True, exist_ok=True)
    with patch("kiro_crew.cli_commands.sel"):
        yield root


def _seed(*records: CronRunRecord) -> None:
    store = CronHistoryStore(base_dir=config_dir())
    for record in records:
        asyncio.run(store.append(record))


def _run(job_id: str, run_id: str, started: float, status: str = "success", **kw):
    return CronRunRecord(
        run_id=run_id,
        job_id=job_id,
        started_at=started,
        finished_at=started + 2.5,
        duration_ms=2500,
        status=status,
        **kw,
    )


def _history(*argv: str) -> None:
    # The real cron parser, not cli.main: that also sets up process-wide logging.
    root = argparse.ArgumentParser(prog="kirocrew")
    register_cron_parser(root.add_subparsers(dest="command"))
    _cron(root.parse_args(["cron", "history", *argv]))


def test_status_error_keeps_only_failed_runs_redacted(home, capsys):
    _seed(
        _run("job1", "r-ok", 1_000.0, summary="all good"),
        _run("job1", "r-bad", 2_000.0, status="failure", error=f"token {SECRET} rejected"),
        _run("job1", "r-ok2", 3_000.0, summary="fine again"),
    )

    _history("job1", "--status", "error", "--json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert [r["run_id"] for r in payload["runs"]] == ["r-bad"]
    assert SECRET not in payload["runs"][0]["error"]
    assert "[REDACTED" in payload["runs"][0]["error"]


def test_run_prints_the_full_record_and_an_unknown_run_exits_1(home, capsys):
    _seed(
        _run(
            "job1",
            "r-bad",
            2_000.0,
            status="failure",
            summary="ran the check",
            error="exit 3",
            trace=f"step one\nleaked {SECRET}\nstep three",
        )
    )

    _history("job1", "--run", "r-bad")
    out = capsys.readouterr().out
    assert "run: r-bad" in out
    assert "status: failure" in out
    assert "trace:\n  step one\n" in out
    assert "  step three" in out
    assert SECRET not in out

    with pytest.raises(SystemExit) as exc:
        _history("job1", "--run", "nope")
    assert exc.value.code == 1
    assert "Run not found: nope" in capsys.readouterr().err


def test_all_with_limit_returns_the_newest_run_across_jobs(home, capsys):
    svc = CronService(base_dir=config_dir())
    first = svc.add_job(name="first job", message="m", every_secs=3600).id
    second = svc.add_job(name="second job", message="m", every_secs=3600).id
    # In finish order, as a run appends: the store's "newest" is the last appended.
    _seed(
        _run(first, "r-old", 1_000.0),
        _run(first, "r-mid", 3_000.0),
        _run(second, "r-new", 5_000.0),
    )

    _history("--all", "-n", "1", "--json")

    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 3
    assert [(r["run_id"], r["job_name"]) for r in payload["runs"]] == [("r-new", "second job")]


def test_a_disabled_store_is_an_error_not_an_empty_history(home, capsys):
    # A regular file where the directory belongs: the store's own unusable-dir path.
    (config_dir() / "cron-history").write_text("", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        _history("job1")

    assert exc.value.code == 1
    out, err = capsys.readouterr()
    assert "No runs recorded" not in out
    assert "Error: cron run history is not readable from this process" in err


def test_an_empty_read_in_a_sandbox_is_an_error_not_an_empty_history(home, capsys, monkeypatch):
    # On Linux the sandbox masks cron-history with an empty writable directory,
    # which the store accepts, so only the confinement evidence tells it apart.
    monkeypatch.setattr("kiro_crew.cli_cron_history._sandbox_confined", lambda: True)

    with pytest.raises(SystemExit) as exc:
        _history("job1", "--status", "error")

    assert exc.value.code == 1
    out, err = capsys.readouterr()
    assert "No runs" not in out
    assert "Error: cron run history is not readable from this process" in err
