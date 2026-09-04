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

import contextlib
import json
import logging
from pathlib import Path

import pytest

from kiro_crew import cron as cron_mod
from kiro_crew.cron import CronService, _job_from_record


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


# --- Narrowing the per-record catch: a code defect is not "bad data" -------
#
# ``CronService._load`` used to catch ``AttributeError`` around
# ``_job_from_record``, which cannot raise it from any JSON-representable
# record (proved by
# ``test_json_shaped_malformations_raise_only_key_or_type_error`` below). The
# rationale, and what catching it costs, lives once in ``_job_from_record``'s
# docstring; these tests pin the behaviour. The read-only
# ``count_enabled_from_disk`` probe keeps the wider tuple -- narrowing that
# site is deliberately out of scope here.


def _boom_on_a_valid_record(_j: dict) -> bool:
    """Stand in for a code defect in the record->job path.

    ``_record_is_enabled`` is called by ``_job_from_record`` for every record,
    so patching it raises on records that are perfectly well-formed -- exactly
    the shape of a refactor or a new validator going wrong.
    """
    raise AttributeError("simulated code defect in the record->job path")


def test_code_defect_does_not_empty_a_live_registry(tmp_path, monkeypatch) -> None:
    """A code defect must not silently truncate a loaded registry.

    DISCRIMINATING: with the blanket catch, both well-formed records are
    reclassified as malformed and ``self._jobs`` becomes empty.
    """
    mgr = CronService(base_dir=tmp_path)
    _write_store(mgr._path, [_good("a"), _good("b")])
    mgr._load()
    assert [j.id for j in mgr._jobs] == ["a", "b"], "precondition: both jobs load cleanly"

    monkeypatch.setattr(cron_mod, "_record_is_enabled", _boom_on_a_valid_record)

    # A reload -- _sync() runs one whenever the file changes on disk.
    with contextlib.suppress(AttributeError):
        mgr._load()

    assert [j.id for j in mgr._jobs] == ["a", "b"]


def test_code_defect_does_not_make_the_loss_permanent(tmp_path, monkeypatch) -> None:
    """The next write must not persist a registry a code defect emptied.

    DISCRIMINATING, and the harm this change exists to prevent: ``_save``
    serialises ``self._jobs`` only, so once a defect has emptied the registry
    the first subsequent write erases both jobs from disk for good.
    """
    mgr = CronService(base_dir=tmp_path)
    _write_store(mgr._path, [_good("a"), _good("b")])
    mgr._load()

    monkeypatch.setattr(cron_mod, "_record_is_enabled", _boom_on_a_valid_record)
    with contextlib.suppress(AttributeError):
        mgr._load()

    mgr._save()  # any later mutation persists whatever the registry now holds

    reread = json.loads(mgr._path.read_text(encoding="utf-8"))
    assert [j["id"] for j in reread["jobs"]] == ["a", "b"]


def test_json_shaped_malformations_raise_only_key_or_type_error() -> None:
    """CHARACTERISATION (passes before and after): ``AttributeError`` is not a
    bad-data signal for this builder, so dropping it from the caught tuple
    costs no malformed-record coverage.

    Every ``.get()`` in the extraction path is dominated by a ``[...]``
    subscript on the same object (``j["id"]`` before any ``j.get(...)``;
    ``j["schedule"]["kind"]`` before any ``j["schedule"].get(...)``). From
    JSON only a ``dict`` survives a string subscript, and a ``dict`` always
    has ``.get`` -- so no ``json.loads`` output can reach the ``.get`` calls
    without a ``dict`` in hand.
    """
    malformed: list[object] = [
        # record itself is not an object
        "garbage",
        ["id", "name"],
        3,
        1.5,
        True,
        None,
        # record is an object but incomplete
        {},
        {"id": "x"},
        {"id": "x", "name": "n", "message": "m"},  # no "schedule"
        {"id": "x", "message": "m", "schedule": {"kind": "every"}},  # no "name"
        # schedule container is the wrong shape
        {"id": "x", "name": "n", "message": "m", "schedule": "every"},
        {"id": "x", "name": "n", "message": "m", "schedule": []},
        {"id": "x", "name": "n", "message": "m", "schedule": 7},
        {"id": "x", "name": "n", "message": "m", "schedule": None},
        {"id": "x", "name": "n", "message": "m", "schedule": {}},  # no "kind"
    ]
    for record in malformed:
        with pytest.raises((KeyError, TypeError)) as caught:
            _job_from_record(record)  # type: ignore[arg-type]
        assert not isinstance(caught.value, AttributeError), record

    # Positive control: the same call on a well-formed record does NOT raise,
    # so the loop above is exercising the real extraction path.
    assert _job_from_record(_good("ok")).id == "ok"


def test_genuine_bad_data_still_skips_and_still_drops_on_the_next_write(tmp_path) -> None:
    """CHARACTERISATION (passes before and after): the documented contract for
    a genuinely malformed record is unchanged.

    ``docs/system-specs/modules/learn-cron-dashboard.md`` states that a
    skipped record is dropped from disk by the first write that follows, and
    the load warning is the operator's recovery window. Narrowing the caught
    tuple must not alter that -- only which exceptions count as bad data.
    """
    mgr = CronService(base_dir=tmp_path)
    bad = {"id": "bad", "message": "m", "schedule": {"kind": "every"}}  # no "name"
    _write_store(mgr._path, [_good("a"), bad])

    mgr._load()
    assert [j.id for j in mgr._jobs] == ["a"]

    mgr._save()

    reread = json.loads(mgr._path.read_text(encoding="utf-8"))
    assert [j["id"] for j in reread["jobs"]] == ["a"]
