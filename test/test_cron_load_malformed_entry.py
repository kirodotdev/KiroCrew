"""Regression tests for #4664: one malformed job entry must not drop the store.

``CronService._load`` used to deserialize the job list in a single
all-or-nothing comprehension inside ``except (json.JSONDecodeError, KeyError)``:
a ``KeyError`` from any ONE entry aborted the whole comprehension and the
handler replaced the registry with an empty list — one malformed or legacy
record silently discarded EVERY job. ``_load`` runs at startup and again from
``_sync`` whenever the file changes externally, so a hand-edit or a future
schema addition could wipe the live registry at runtime.

The fix parses entries independently (``_job_from_record`` built per entry in
its own try block, malformed ones warned about and skipped) and reserves the
whole-store reset for a genuinely unparseable file (``json.JSONDecodeError``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from kiro_crew.cron import CronService


def _write_store(path: Path, jobs: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "jobs": jobs}), encoding="utf-8")


def _good(job_id: str) -> dict:
    return {
        "id": job_id,
        "name": f"job-{job_id}",
        "message": "m",
        "schedule": {"kind": "every", "every_secs": 60},
    }


def test_malformed_entry_is_skipped_and_good_jobs_survive(tmp_path, caplog) -> None:
    """One record missing a required key loses only itself, never its neighbors."""
    mgr = CronService(base_dir=tmp_path)
    bad = {"id": "bad", "message": "m", "schedule": {"kind": "every"}}  # no "name"
    _write_store(mgr._path, [_good("a"), bad, _good("b")])

    with caplog.at_level(logging.WARNING, logger="kiro_crew.cron"):
        mgr._load()

    assert [j.id for j in mgr._jobs] == ["a", "b"]
    assert any("Skipping malformed cron job entry" in r.message for r in caplog.records)


def test_non_object_entry_is_skipped(tmp_path) -> None:
    """A non-dict entry (would raise TypeError, previously uncaught) is skipped."""
    mgr = CronService(base_dir=tmp_path)
    _write_store(mgr._path, [_good("a"), "garbage", _good("b")])

    mgr._load()

    assert [j.id for j in mgr._jobs] == ["a", "b"]


def test_missing_schedule_kind_is_skipped(tmp_path) -> None:
    """A record whose schedule container lacks ``kind`` loses only itself."""
    mgr = CronService(base_dir=tmp_path)
    bad = {"id": "bad", "name": "n", "message": "m", "schedule": {}}
    _write_store(mgr._path, [bad, _good("a")])

    mgr._load()

    assert [j.id for j in mgr._jobs] == ["a"]


def test_unparseable_file_still_resets_whole_store(tmp_path, caplog) -> None:
    """A file that is not valid JSON keeps the existing whole-store reset."""
    mgr = CronService(base_dir=tmp_path)
    mgr._path.parent.mkdir(parents=True, exist_ok=True)
    mgr._path.write_text("{not json", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="kiro_crew.cron"):
        mgr._load()

    assert mgr._jobs == []
    assert any("Failed to load cron store" in r.message for r in caplog.records)


def test_loaded_fields_roundtrip_through_extracted_builder(tmp_path) -> None:
    """The extracted ``_job_from_record`` preserves the derived-enabled and
    default semantics the inline comprehension had (auto-pause survives reload,
    legacy ``!enabled`` fallback maps to ``user_paused``)."""
    mgr = CronService(base_dir=tmp_path)
    _write_store(
        mgr._path,
        [
            {**_good("auto"), "auto_paused": True},
            {**_good("legacy"), "enabled": False},
            _good("on"),
        ],
    )

    mgr._load()

    by_id = {j.id: j for j in mgr._jobs}
    assert not by_id["auto"].enabled and by_id["auto"].auto_paused
    assert not by_id["legacy"].enabled and by_id["legacy"].user_paused
    assert by_id["on"].enabled


def test_count_enabled_from_disk_survives_non_object_entry(tmp_path) -> None:
    """The sibling reader shares the skip decision: a non-dict entry must not
    crash ``count_enabled_from_disk`` (an ``AttributeError`` here would escape
    its ``except (OSError, json.JSONDecodeError)`` and silently kill the WS
    status pusher that calls this reader off the event loop)."""
    mgr = CronService(base_dir=tmp_path)
    _write_store(mgr._path, [_good("a"), "garbage", _good("b")])

    assert mgr.count_enabled_from_disk() == 2


def test_count_enabled_from_disk_does_not_count_records_load_skips(tmp_path) -> None:
    """A malformed dict record the scheduler refuses to load is not counted —
    the two readers of the store must not drift."""
    mgr = CronService(base_dir=tmp_path)
    bad = {"id": "bad", "message": "m", "schedule": {"kind": "every"}}  # no "name"
    _write_store(mgr._path, [_good("a"), bad])

    assert mgr.count_enabled_from_disk() == 1


def test_top_level_non_object_resets_store_and_counts_zero(tmp_path, caplog) -> None:
    """A document that parses but holds no jobs list — top-level "[]", a
    scalar, or {"jobs": null} — is treated as unsalvageable by BOTH readers:
    ``_load`` resets to an empty registry with a warning instead of raising
    ``AttributeError``/``TypeError``, and ``count_enabled_from_disk`` returns
    0 instead of crashing the WS pusher."""
    mgr = CronService(base_dir=tmp_path)
    mgr._path.parent.mkdir(parents=True, exist_ok=True)
    for payload in ("[]", '{"jobs": null}', '{"jobs": 3}'):
        mgr._path.write_text(payload, encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.cron"):
            mgr._load()

        assert mgr._jobs == [], payload
        assert any("Failed to load cron store" in r.message for r in caplog.records)
        assert mgr.count_enabled_from_disk() == 0, payload
