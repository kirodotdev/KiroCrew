"""A cron store written by a newer Kiro Crew keeps its newer data through this build's save.

The desktop updater allows downgrades, so a store a newer build wrote can be loaded and
saved by this one. Each test writes a store with the current code, edits it the way a
newer build plausibly writes it, loads it with a fresh ``CronService`` on a ``tmp_path``
home and makes one ordinary write. No cleanup, sweep, prune or reap function is called.
"""

from __future__ import annotations

import json
import logging
import time

from kiro_crew.cron import CronService


def _id(job) -> str:
    return job.id if hasattr(job, "id") else str(job)


def _service(tmp_path, monkeypatch) -> CronService:
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    return CronService(base_dir=tmp_path)


def _edit(path, edit) -> None:
    doc = json.loads(path.read_text(encoding="utf-8"))
    edit(doc)
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _jobs_by_id(path) -> tuple[dict, dict]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    return doc, {rec["id"]: rec for rec in doc["jobs"]}


def test_an_older_builds_save_keeps_a_newer_builds_fields_and_version(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    a = _id(svc.add_job(name="plain", message="m", cron_expr="0 9 * * *"))
    b = _id(svc.add_job(name="new-field", message="m", cron_expr="0 10 * * *"))
    path = tmp_path / "crons.json"

    def newer(doc):
        doc["version"] = 3
        doc["defaults"] = {"timezone": "UTC"}
        for rec in doc["jobs"]:
            if rec["id"] == b:
                rec["retry_policy"] = {"max_attempts": 3}
                rec["schedule"]["jitter_secs"] = 30

    _edit(path, newer)
    older = CronService(base_dir=tmp_path)  # the rolled-back build
    older.enable_job(a, enabled=False)  # one ordinary write, to an unrelated job
    doc, by_id = _jobs_by_id(path)
    kept = by_id[b].get("retry_policy")
    assert kept == {"max_attempts": 3}, f"the newer job field was dropped: {by_id[b]}"
    assert by_id[b]["schedule"].get("jitter_secs") == 30, by_id[b]["schedule"]
    assert doc.get("defaults") == {"timezone": "UTC"}, "a newer top-level key was dropped"
    assert doc["version"] == 3, f"the store version was lowered to {doc['version']}"
    assert by_id[a]["user_paused"] is True, "the ordinary write itself landed"


def test_a_store_from_this_version_round_trips_unchanged(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    svc.add_job(name="plain", message="m", cron_expr="0 9 * * *")
    a = _id(svc.add_job(name="other", message="m", every_secs=600))
    path = tmp_path / "crons.json"
    older = CronService(base_dir=tmp_path)
    older.enable_job(a, enabled=False)
    older.enable_job(a, enabled=True)
    before = path.read_bytes()
    CronService(base_dir=tmp_path).enable_job(a, enabled=True)
    assert path.read_bytes() == before
    assert json.loads(before)["version"] == 2


def test_a_known_field_this_build_changes_is_written_with_its_new_value(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    a = _id(svc.add_job(name="plain", message="m", cron_expr="0 9 * * *"))
    path = tmp_path / "crons.json"

    def newer(doc):
        for rec in doc["jobs"]:
            rec["retry_policy"] = {"max_attempts": 3}
            rec["user_paused"] = False

    _edit(path, newer)
    CronService(base_dir=tmp_path).enable_job(a, enabled=False)
    _doc, by_id = _jobs_by_id(path)
    assert by_id[a]["user_paused"] is True, "a known field keeps this build's new value"
    assert by_id[a]["enabled"] is False
    assert by_id[a]["retry_policy"] == {"max_attempts": 3}


def test_a_known_field_this_build_clears_stays_cleared(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    a = _id(svc.add_job(name="plain", message="m", cron_expr="0 9 * * *"))
    path = tmp_path / "crons.json"
    _edit(path, lambda doc: doc["jobs"][0].update({"managed_by": "installer:x"}))
    older = CronService(base_dir=tmp_path)
    job = next(j for j in older.list_jobs(include_disabled=True) if _id(j) == a)
    assert job.managed_by == "installer:x"
    job.managed_by = ""
    with older._file_lock():
        older._save()
    _doc, by_id = _jobs_by_id(path)
    assert "managed_by" not in by_id[a], "a value this build cleared is not restored"


def test_a_job_this_build_deletes_is_gone_with_its_unknown_keys(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    keep = _id(svc.add_job(name="keep", message="m", cron_expr="0 9 * * *"))
    gone = _id(svc.add_job(name="gone", message="m", cron_expr="0 10 * * *"))
    path = tmp_path / "crons.json"

    def newer(doc):
        for rec in doc["jobs"]:
            rec["retry_policy"] = {"max_attempts": 3}

    _edit(path, newer)
    assert CronService(base_dir=tmp_path).remove_job(gone, actor="test", source="test")
    text = path.read_text(encoding="utf-8")
    _doc, by_id = _jobs_by_id(path)
    assert sorted(by_id) == [keep]
    assert by_id[keep]["retry_policy"] == {"max_attempts": 3}
    assert gone not in text


def test_a_job_with_an_unknown_schedule_kind_still_never_fires(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    c = _id(svc.add_job(name="new-kind", message="m", cron_expr="* * * * *"))
    path = tmp_path / "crons.json"
    _edit(path, lambda doc: doc["jobs"][0]["schedule"].update({"kind": "jittered"}))
    older = CronService(base_dir=tmp_path)
    job = next(j for j in older.list_jobs(include_disabled=True) if _id(j) == c)
    assert CronService._is_due(job, time.time()) is False
    assert CronService._is_due(job, time.time() + 3600) is False
    older.enable_job(c, enabled=True)
    _doc, by_id = _jobs_by_id(path)
    assert by_id[c]["schedule"]["kind"] == "jittered"


def _store_with_newer_fields(tmp_path, monkeypatch, fields_by_name):
    """One job per name, saved by this build, then given that name's newer keys."""
    svc = _service(tmp_path, monkeypatch)
    ids = {n: _id(svc.add_job(name=n, message="m", cron_expr="0 9 * * *")) for n in fields_by_name}
    path = tmp_path / "crons.json"

    def newer(doc):
        for rec in doc["jobs"]:
            for name, added in fields_by_name.items():
                if rec["id"] == ids[name]:
                    rec.update(added)

    _edit(path, newer)
    return ids, path


def _kept_bytes(unknown) -> int:
    return sum(len(json.dumps(dict(part))) for part in (unknown.job, unknown.schedule) if part)


def test_a_16_mib_field_from_a_newer_build_is_not_kept_in_memory(tmp_path, monkeypatch, caplog):
    """The over-cap case: that job keeps none of its newer keys, says so, and saves without them."""
    huge = {"blob": "x" * (16 * 1024 * 1024)}
    ids, path = _store_with_newer_fields(
        tmp_path, monkeypatch, {"huge": huge, "small": {"retry_policy": {"max_attempts": 3}}}
    )
    older = CronService(base_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.cron"):
        older.enable_job(ids["small"], enabled=False)  # loads, then one ordinary write
    kept = dict(older._unknown_fields)
    assert (
        ids["huge"] not in kept
    ), f"the 16 MiB field stayed in memory: {_kept_bytes(kept[ids['huge']])} bytes"
    assert kept[ids["small"]].job == {
        "retry_policy": {"max_attempts": 3}
    }, "a job under the cap lost its key"
    from kiro_crew.cron_service.store import MAX_UNKNOWN_FIELDS_BYTES

    assert all(_kept_bytes(u) <= MAX_UNKNOWN_FIELDS_BYTES for u in kept.values())
    assert "over the" in caplog.text, "the refused job was not logged"
    _doc, by_id = _jobs_by_id(path)
    assert "blob" not in by_id[ids["huge"]]
    assert by_id[ids["small"]]["retry_policy"] == {"max_attempts": 3}


def test_the_cap_keeps_a_job_at_the_cap_and_refuses_one_byte_over(tmp_path, monkeypatch):
    from kiro_crew.cron_service.store import MAX_UNKNOWN_FIELDS_BYTES

    fill = MAX_UNKNOWN_FIELDS_BYTES - len(json.dumps({"blob": ""}))
    ids, path = _store_with_newer_fields(
        tmp_path, monkeypatch, {"at": {"blob": "x" * fill}, "over": {"blob": "x" * (fill + 1)}}
    )
    older = CronService(base_dir=tmp_path)
    older.enable_job(ids["at"], enabled=False)
    kept = dict(older._unknown_fields)
    assert _kept_bytes(kept[ids["at"]]) == MAX_UNKNOWN_FIELDS_BYTES
    assert ids["over"] not in kept
    _doc, by_id = _jobs_by_id(path)
    assert len(by_id[ids["at"]]["blob"]) == fill
    assert "blob" not in by_id[ids["over"]]


def test_a_16_mib_top_level_key_from_a_newer_build_is_not_kept(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    a = _id(svc.add_job(name="plain", message="m", cron_expr="0 9 * * *"))
    path = tmp_path / "crons.json"
    _edit(path, lambda doc: doc.update({"version": 3, "blob": "x" * (16 * 1024 * 1024)}))
    older = CronService(base_dir=tmp_path)
    older.enable_job(a, enabled=False)
    assert dict(older._store_extra) == {}, "the 16 MiB top-level key stayed in memory"
    doc, _by_id = _jobs_by_id(path)
    assert "blob" not in doc
    assert doc["version"] == 3, "the version is kept whatever the top-level keys"


def test_many_jobs_past_the_store_total_keep_only_what_fits(tmp_path, monkeypatch, caplog):
    """40 jobs of 60 KB each: under the per-job cap one by one, 2.4 MB together."""
    try:
        from kiro_crew.cron_service.store import MAX_UNKNOWN_FIELDS_STORE_BYTES
    except ImportError:  # no store total: assert against the intended one
        MAX_UNKNOWN_FIELDS_STORE_BYTES = 1024 * 1024

    names = [f"j{i:02d}" for i in range(40)]
    ids, path = _store_with_newer_fields(
        tmp_path, monkeypatch, {name: {"blob": "x" * 60_000} for name in names}
    )
    older = CronService(base_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.cron"):
        older.enable_job(ids["j00"], enabled=False)  # loads, then one ordinary write
    kept = dict(older._unknown_fields)
    total = sum(_kept_bytes(u) for u in kept.values())
    assert total <= MAX_UNKNOWN_FIELDS_STORE_BYTES, f"{total} bytes kept for one store"
    in_order = [ids[name] in kept for name in names]
    fits = MAX_UNKNOWN_FIELDS_STORE_BYTES // len(json.dumps({"blob": "x" * 60_000}))
    assert in_order == [True] * fits + [False] * (len(names) - fits), in_order
    assert f"{len(names) - fits} job(s) carry keys from a newer build past the" in caplog.text
    _doc, by_id = _jobs_by_id(path)
    assert "blob" in by_id[ids[names[0]]] and "blob" not in by_id[ids[names[-1]]]
