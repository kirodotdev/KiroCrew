"""Regression tests: one malformed job entry must not drop the store.

A naive ``CronService._load`` would deserialize the job list in a single
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
    """A non-dict entry (would raise TypeError if uncaught) is skipped."""
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
# ``CronService._load`` must not catch ``AttributeError`` around
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


# --- Corrupted-store resilience: non-string values in string-typed fields must not crash the listing ---

# Metadata str fields _job_from_record reads with .get() -> coerced to "" + WARNING.
_COERCED_STR_FIELDS = [
    "approval_mode",
    "created_by",
    "session_key",
    "last_posted_hash",
    "last_failure_hash",
    "folder_id",
    "model",
    "timezone",
    "last_result_stamp",
    "secret_env_pin",
    "secret_env_pending_pin",
    "source_preset",
    "source_template_prompt",
]

# Optional[str] fields -> coerced to None + WARNING.
_COERCED_OPT_STR_FIELDS = ["channel", "thread_ts", "last_status", "last_error", "last_result"]

# Execution and memory-identity selectors: a present non-string is MALFORMED
# (record skipped), never coerced — a script job whose selector degraded to ""
# would silently become an LLM agent job (mode fallthrough in _cron_callback),
# and a member binding coerced to "" would run the job with its memory
# identity silently stripped (resolve_cron_memory raises on malformed
# bindings rather than falling back).
_SELECTOR_FIELDS = ["script", "command", "agent_id", "member_id", "memory_store"]


def _corrupted_metadata(job_id: str) -> dict:
    """A record storing a non-string in every COERCIBLE guarded field."""
    rec = _good(job_id)
    for i, name in enumerate(_COERCED_STR_FIELDS):
        rec[name] = i if i % 2 == 0 else None  # numbers and nulls, both non-str
    for i, name in enumerate(_COERCED_OPT_STR_FIELDS):
        rec[name] = 12.5 if i % 2 == 0 else ["x"]
    return rec


def test_non_string_metadata_fields_coerce_and_warn(tmp_path, caplog) -> None:
    """A record with a number/null in each coercible field loads —
    str fields coerce to "", Optional[str] fields to None — and one WARNING
    names the record and every field whose stored VALUE was destroyed, since
    the next _save() replaces it on disk (the log line is the recovery
    window, mirroring the malformed-entry skip warning)."""
    import logging

    mgr = CronService(base_dir=tmp_path)
    good = {**_good("ok"), "approval_mode": "auto", "channel": "slack"}
    _write_store(mgr._path, [_corrupted_metadata("bad"), good])

    with caplog.at_level(logging.WARNING, logger="kiro_crew.cron"):
        mgr._load()

    by_id = {j.id: j for j in mgr._jobs}
    assert set(by_id) == {"bad", "ok"}
    for name in _COERCED_STR_FIELDS:
        assert getattr(by_id["bad"], name) == "", name
    for name in _COERCED_OPT_STR_FIELDS:
        assert getattr(by_id["bad"], name) is None, name
    assert by_id["ok"].approval_mode == "auto"
    assert by_id["ok"].channel == "slack"
    warn = next(r for r in caplog.records if "Coercing non-string value" in r.message)
    rendered = warn.getMessage()
    assert "'bad'" in rendered
    # Value-destroying coercions are named; a stored null carries no value to
    # lose, so only the non-null non-string fields must appear.
    for i, name in enumerate(_COERCED_STR_FIELDS):
        if i % 2 == 0:
            assert name in rendered, name
    for name in _COERCED_OPT_STR_FIELDS:
        assert name in rendered, name


@pytest.mark.parametrize("field", _SELECTOR_FIELDS)
def test_non_string_execution_selector_skips_record(tmp_path, field) -> None:
    """Fail closed: a non-string execution selector must SKIP the
    record, never coerce — coercing `script: 7` to "" would silently flip a
    script job into an LLM agent job executing its message."""
    mgr = CronService(base_dir=tmp_path)
    bad = {**_good("bad"), field: 7}
    _write_store(mgr._path, [bad, _good("ok")])

    mgr._load()

    assert [j.id for j in mgr._jobs] == ["ok"]


@pytest.mark.parametrize(
    "mutate",
    [
        {"name": 123},
        {"message": None},
        {"id": 5},
        {"schedule": {"kind": 1, "every_secs": 60}},
        {"schedule": {"kind": "every", "every_secs": "60"}},
        {"schedule": {"kind": "every", "every_secs": float("nan")}},
        {"schedule": {"kind": "at", "at_ts": float("inf")}},
        {"schedule": {"kind": "at", "at_ts": 1e309}},
        {"schedule": {"kind": "at", "at_ts": 10**400}},
        {"schedule": {"kind": "cron", "cron_expr": 5}},
        {"agent_sequence": [1]},
        {"agent_sequence": "not-a-list"},
        {"agent_sequence": None},
        {"skip_dates": [20260101]},
        {"skip_dates": None},
    ],
    ids=[
        "name",
        "message",
        "id",
        "schedule.kind",
        "schedule.every_secs",
        "schedule.every_secs_nan",
        "schedule.at_ts_inf",
        "schedule.at_ts_1e309",
        "schedule.at_ts_bignum",
        "schedule.cron_expr",
        "agent_seq_member",
        "agent_seq_type",
        "agent_seq_explicit_null",
        "skip_dates_member",
        "skip_dates_explicit_null",
    ],
)
def test_non_string_required_or_list_fields_skip_record(tmp_path, mutate) -> None:
    """Mistyped required identity/payload fields and list fields the
    listing iterates are malformed — the record is skipped whole and the
    sibling survives."""
    mgr = CronService(base_dir=tmp_path)
    _write_store(mgr._path, [{**_good("bad"), **mutate}, _good("ok")])

    mgr._load()

    assert [j.id for j in mgr._jobs] == ["ok"]


@pytest.mark.asyncio
async def test_listing_renders_despite_corrupted_records(tmp_path) -> None:
    """GET /api/crons pipes string fields through redact_* helpers that
    raise on non-string input — corrupted records (coerced or skipped) must
    not 500 the listing for every job."""
    import json as _json
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import api_crons

    mgr = CronService(base_dir=tmp_path)
    _write_store(
        mgr._path,
        [
            _corrupted_metadata("coerced"),
            {**_good("skipped"), "script": 7},
            {**_good("skipped2"), "name": 123},
            _good("ok"),
        ],
    )
    mgr._load()

    state = MagicMock()
    state.crons = mgr
    state.has_slot = MagicMock(return_value=False)
    request = MagicMock()
    request.app = {"state": state}

    resp = await api_crons(request)

    assert resp.status == 200
    jobs = _json.loads(resp.body)["jobs"]
    assert {j["id"] for j in jobs} == {"coerced", "ok"}


def test_probe_readers_do_not_repeat_the_coercion_warning(tmp_path, caplog) -> None:
    """The coercion WARNING is _load's recovery window, emitted once
    per load. count_enabled_from_disk runs _job_from_record as a loadability
    PROBE at WS status-pusher cadence (every push cycle, per connected
    dashboard) and never precedes a store rewrite — it must not re-emit the
    warning indefinitely for one bad record sitting in the store."""
    import logging

    mgr = CronService(base_dir=tmp_path)
    _write_store(mgr._path, [_corrupted_metadata("bad"), _good("ok")])

    with caplog.at_level(logging.WARNING, logger="kiro_crew.cron"):
        assert mgr.count_enabled_from_disk() == 2
        assert mgr.count_enabled_from_disk() == 2

    assert not any("Coercing non-string value" in r.message for r in caplog.records)


def test_every_string_typed_field_is_guarded_completeness_pin(tmp_path) -> None:
    """Completeness pin: the type invariant — after _load,
    every str-typed CronJob field holds its declared type — is asserted for
    EVERY field discovered from the dataclass itself, so a future field added
    with a bare j.get() read fails here instead of shipping a third
    per-incident guard round for each newly corrupted field. A corrupted field
    satisfies the invariant either way it is handled: coerced (value becomes
    ""/None) or skipped (the record never loads)."""
    import dataclasses

    from kiro_crew.cron import CronJob

    str_field_names = [
        f.name
        for f in dataclasses.fields(CronJob)
        if f.type in ("str", "str | None", "Optional[str]")
    ]
    assert len(str_field_names) >= 24  # confidence check: introspection actually found the fields

    for name in str_field_names:
        mgr = CronService(base_dir=tmp_path / name)
        _write_store(mgr._path, [{**_good("bad"), name: 123}, _good("ok")])

        mgr._load()  # must never raise out of the per-entry isolation

        assert any(j.id == "ok" for j in mgr._jobs), name
        for j in mgr._jobs:
            value = getattr(j, name)
            assert value is None or isinstance(
                value, str
            ), f"non-string survived deserialization in field {name!r}: {value!r}"


@pytest.mark.parametrize(
    "at_ts",
    [1e18, -1, 1.75e12],
    ids=["1e18", "negative", "epoch_millis"],
)
@pytest.mark.asyncio
async def test_extreme_schedule_values_load_and_render_without_dropping(tmp_path, at_ts) -> None:
    """FINITE extreme values are tolerated at the render
    site, never bounded in the deserializer. An extreme stored at_ts (epoch
    milliseconds, beyond-year-9999, negative, bignum) must (a) load
    -- a reader-side value bound not mirrored by the writer silently drops
    jobs and the next _save erases them, the data-loss class GPT fenced --
    and (b) render through GET /api/crons via format_schedule's fallback
    string instead of raising inside the every-job comprehension. Non-finite
    values are the OPPOSITE case: type-shape malformed (no writer can produce
    one, NaN breaks comparison ordering on the fire path), so they live in
    the skip-record group above."""
    import json as _json
    from unittest.mock import MagicMock

    from kiro_crew.dashboard.handlers import api_crons

    mgr = CronService(base_dir=tmp_path)
    poisoned = {**_good("poisoned"), "schedule": {"kind": "at", "at_ts": at_ts}}
    _write_store(mgr._path, [poisoned, _good("ok")])

    mgr._load()  # (a) never raises, record NOT skipped

    assert {j.id for j in mgr._jobs} == {"poisoned", "ok"}

    state = MagicMock()
    state.crons = mgr
    state.has_slot = MagicMock(return_value=False)
    request = MagicMock()
    request.app = {"state": state}

    resp = await api_crons(request)  # (b) listing renders both jobs

    assert resp.status == 200
    jobs = _json.loads(resp.body)["jobs"]
    assert {j["id"] for j in jobs} == {"poisoned", "ok"}


def test_build_job_refuses_non_representable_schedule_numerics(tmp_path) -> None:
    """The writer mirror: the deserializer may skip a record with a
    non-representable schedule numeric precisely because no write path can
    persist one -- _build_job is the persistence chokepoint (covering CLI,
    MCP, dashboard, apps SDK, and both onboarding-import branches) that keeps
    reader and writer exactly aligned, so the skip drops no writer-producible
    record. Bignum ints are refused too: int-float arithmetic on the
    timer-arming path (`at_ts - now`) raises OverflowError at gateway
    startup."""
    mgr = CronService(base_dir=tmp_path)
    with pytest.raises(ValueError, match="representable"):
        mgr._build_job("j", "m", at_ts=float("nan"))
    with pytest.raises(ValueError, match="representable"):
        mgr._build_job("j", "m", every_secs=float("inf"))
    with pytest.raises(ValueError, match="representable"):
        mgr._build_job("j", "m", at_ts=10**400)
    with pytest.raises(ValueError, match="representable"):
        mgr._build_job("j", "m", every_secs=10**400)


def test_update_job_refuses_non_representable_interval(tmp_path) -> None:
    """The second writer chokepoint: int() accepts a bignum that
    clears the >= 60 bound, and int(float("inf")) raises OverflowError
    outside the old (ValueError, TypeError) tuple -- both must refuse."""
    mgr = CronService(base_dir=tmp_path)
    job = mgr.add_job("j", "m", every_secs=3600)
    with pytest.raises(ValueError, match="Invalid interval"):
        mgr._update_job_locked(job.id, every_secs=10**400)
    with pytest.raises(ValueError, match="Invalid interval"):
        mgr._update_job_locked(job.id, every_secs=float("inf"))


def test_compute_next_run_ts_never_returns_non_finite(tmp_path) -> None:
    """Serialize-site tolerance: a finite stored every_secs=1e308 sums
    to inf at `last + every_secs`; the raw result must degrade to None before
    it reaches the GET /api/crons payload, where json.dumps(allow_nan=True)
    would emit the invalid-JSON token Infinity and break the whole listing
    client-side."""
    from kiro_crew.cron import CronJob, CronSchedule, compute_next_run_ts

    job = CronJob(
        id="j",
        name="n",
        message="m",
        schedule=CronSchedule(kind="every", every_secs=1.7e308),
        created_ts=1.7e308,
        last_run_ts=1.7e308,  # last + every_secs overflows float to inf
    )
    assert compute_next_run_ts(job, now=2000.0) is None


# Matrix pin: every numeric CronJob field must hold a representable
# finite number (or its declared unset value) the moment _load returns.
# Field axis is enumerated from the dataclass so a new numeric field fails
# until it is classified HERE; the two schedule fields (skip-record) live in
# CronSchedule and are pinned by the skip-group parametrize above.
_NUMERIC_FIELD_DEFAULTS = {
    "last_run_ts": None,
    "created_ts": 0.0,
    "last_result_ts": 0.0,
    "last_posted_at": 0.0,
    "last_failure_at": 0.0,
    "secret_env_pending_ts": 0.0,
    "consecutive_dupes": 0,
    "consecutive_failures": 0,
    "timeout_secs": 1800,  # _JOB_TIMEOUT_SECS
    "timeout": 0,
}

_NUMERIC_POISONS = [float("nan"), float("inf"), 10**400, "60", True, None]


@pytest.mark.asyncio
async def test_numeric_field_consumer_matrix_pin(tmp_path) -> None:
    """The invariant that closes the (field x consumer) hazard matrix.
    Field axis: every numeric CronJob field, enumerated from the dataclass,
    poisoned with each shape json.loads can produce -- must load without
    raising and hold its declared default afterwards. Consumer axis: with
    the poison planted, the timer, due-decision, next-run, render, and JSON
    envelope consumers are all driven; the api_crons payload must contain no
    NaN/Infinity token (checked via json.loads parse_constant, the
    machine-readable spelling of 'valid JSON')."""
    import dataclasses
    import json as _json
    from unittest.mock import MagicMock

    from kiro_crew.cron import CronJob, compute_next_run_ts, format_schedule
    from kiro_crew.dashboard.handlers import api_crons

    numeric_fields = {
        f.name
        for f in dataclasses.fields(CronJob)
        if f.type in ("int", "float", "int | None", "float | None")
        and f.name not in ("fire_time_denied", "run_never_started")
    }
    assert numeric_fields == set(_NUMERIC_FIELD_DEFAULTS), (
        "a numeric CronJob field is not classified in _NUMERIC_FIELD_DEFAULTS -- "
        "decide skip/coerce for it before shipping"
    )

    def _reject_constant(const: str) -> None:
        raise AssertionError(f"invalid JSON token {const} reached the api_crons payload")

    for field, default in _NUMERIC_FIELD_DEFAULTS.items():
        for poison in _NUMERIC_POISONS:
            mgr = CronService(base_dir=tmp_path / f"{field}-{id(poison)}-{len(repr(poison))}")
            _write_store(mgr._path, [{**_good("bad"), field: poison}, _good("ok")])

            mgr._load()  # never raises out of per-entry isolation

            by_id = {jb.id: jb for jb in mgr._jobs}
            assert set(by_id) == {"bad", "ok"}, (field, poison)
            assert getattr(by_id["bad"], field) == default or (
                default is None and getattr(by_id["bad"], field) is None
            ), (field, poison)

            # Consumer axis: none of these may raise with the poison planted.
            mgr._next_wake_secs()
            for jb in mgr._jobs:
                cron_mod.CronService._is_due(jb, 2_000_000_000.0)
                compute_next_run_ts(jb)
                format_schedule(jb.schedule, tz_name="UTC")

            state = MagicMock()
            state.crons = mgr
            state.has_slot = MagicMock(return_value=False)
            request = MagicMock()
            request.app = {"state": state}
            resp = await api_crons(request)
            assert resp.status == 200, (field, poison)
            _json.loads(resp.body, parse_constant=_reject_constant)


def test_explicit_null_numeric_warns_unless_none_is_the_declared_unset(tmp_path, caplog) -> None:
    """An explicit null in a numeric field is a present malformed value and
    joins the coercion warning -- except where the declared unset IS None
    (last_run_ts), which a writer legitimately serializes for a job that
    never ran and must load silently."""
    mgr = CronService(base_dir=tmp_path)
    _write_store(
        mgr._path,
        [{**_good("a"), "created_ts": None, "last_run_ts": None}],
    )

    with caplog.at_level(logging.WARNING, logger="kiro_crew.cron"):
        mgr._load()

    warnings = [r.getMessage() for r in caplog.records if "Coercing" in r.getMessage()]
    assert len(warnings) == 1 and "created_ts" in warnings[0]
    assert "last_run_ts" not in warnings[0]
    job = mgr._jobs[0]
    assert job.created_ts == 0.0 and job.last_run_ts is None


def test_mcp_next_run_render_degrades_on_extreme_at_ts(tmp_path) -> None:
    """cron_list's next-run formatter degrades an unrenderable (representable
    but beyond-strftime-range) timestamp instead of raising inside the loop
    that renders every job -- same posture as format_schedule and the CLI."""
    from kiro_crew.mcp_cron import _format_next_run

    mgr = CronService(base_dir=tmp_path)
    _write_store(
        mgr._path,
        [{**_good("far"), "schedule": {"kind": "at", "at_ts": 4.0e11}}],
    )
    mgr._load()
    (job,) = mgr._jobs

    out = _format_next_run(job, now=0.0, local_tz=None)

    assert "invalid stored time" in out
