"""Tests for the table-driven string-field validation at the cron persistence
chokepoint (issue #5782).

``_build_job`` and ``_update_job_locked`` now iterate a single
``_CRON_STRING_FIELD_CAPS`` table to enforce type+length on every
caller-supplied string field. These tests lock that guarantee:

- All fields in the table reject non-string truthy values (type gate).
- All fields in the table reject strings exceeding their cap (length gate).
- A value at the exact cap is accepted wherever the surface actually persists
  it (all fields on create; the updatable subset on update; timezone via a
  real IANA zone, since its semantic check rejects synthetic strings).
- Falsy values (None, "") in an update are no-ops (falsy-skip semantics).
- An anti-drift test enumerates CronJob str fields and asserts every
  persisted caller-supplied field is covered by the validation table.
- A typo guard asserts every name in the table is an actual CronJob field.
"""

from __future__ import annotations

import dataclasses

import pytest

from kiro_crew.cron import (
    _CRON_STRING_FIELD_CAPS,
    CronJob,
    CronService,
)
from kiro_crew.validation import CHANNEL_MAX_LEN, CRON_ADD_SCHEMA, MAX_SHORT_STRING

# Derived from the table (its single source of truth) — the set the anti-drift
# test checks CronJob's fields against.
_VALIDATED_FIELD_NAMES: frozenset[str] = frozenset(name for name, _ in _CRON_STRING_FIELD_CAPS)

# ── Fixtures ──


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


# ── Per-field parametrize data ──
# Fields NOT already pinned by test_cron_name_cap.py / test_cron_message_cap.py.
# Each entry: (field_name, cap, oversize_value, non_string_value)
_NEWLY_VALIDATED_FIELDS: list[tuple[str, int, str, object]] = [
    ("channel", CHANNEL_MAX_LEN, "x" * (CHANNEL_MAX_LEN + 1), 42),
    ("thread_ts", 30, "x" * 31, ["ts"]),
    ("agent_id", MAX_SHORT_STRING, "x" * (MAX_SHORT_STRING + 1), 123),
    ("created_by", MAX_SHORT_STRING, "x" * (MAX_SHORT_STRING + 1), {"uid": 1}),
    ("folder_id", MAX_SHORT_STRING, "x" * (MAX_SHORT_STRING + 1), True),
    ("session_key", MAX_SHORT_STRING, "x" * (MAX_SHORT_STRING + 1), 99.9),
    ("model", MAX_SHORT_STRING, "x" * (MAX_SHORT_STRING + 1), ["m"]),
    ("command", 5000, "x" * 5001, 0xDEAD),
    ("script", 200, "x" * 201, ("t",)),
    ("timezone", 50, "x" * 51, 7),
]

_FIELD_IDS = [f[0] for f in _NEWLY_VALIDATED_FIELDS]

# Exact-cap acceptance needs the value to survive the field's SEMANTIC checks
# too, not just the type/length gate. timezone is excluded: is_valid_timezone
# rejects any synthetic 50-char string, so its cap boundary is untestable
# through the public API (a dedicated real-timezone test below keeps the
# semantic acceptance covered).
_ADD_EXACT_CAP_FIELDS = [f for f in _NEWLY_VALIDATED_FIELDS if f[0] != "timezone"]
_ADD_EXACT_CAP_IDS = [f[0] for f in _ADD_EXACT_CAP_FIELDS]

# _update_job_locked validates every table field present in kwargs but only
# ASSIGNS the documented updatable ones. Exact-cap acceptance on update is
# therefore only meaningful for fields update actually persists; the rest
# (created_by, thread_ts, session_key, command, script) are validated-then-
# ignored, which the reject tests already pin via their job-unchanged asserts.
_UPDATE_ASSIGNED_FIELDS = [
    f for f in _NEWLY_VALIDATED_FIELDS if f[0] in ("agent_id", "channel", "folder_id", "model")
]
_UPDATE_ASSIGNED_IDS = [f[0] for f in _UPDATE_ASSIGNED_FIELDS]


# ── Build-job (add_job) tests ──


class TestBuildJobFieldValidation:
    """_build_job rejects non-string and oversize values for each field."""

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _NEWLY_VALIDATED_FIELDS,
        ids=_FIELD_IDS,
    )
    def test_add_job_rejects_oversize(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="max length"):
            svc.add_job(name="j", message="m", every_secs=3600, **{field_name: oversize_value})
        assert svc.list_jobs() == []

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _NEWLY_VALIDATED_FIELDS,
        ids=_FIELD_IDS,
    )
    def test_add_job_rejects_non_string(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="must be a string"):
            svc.add_job(name="j", message="m", every_secs=3600, **{field_name: non_string_value})
        assert svc.list_jobs() == []

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _ADD_EXACT_CAP_FIELDS,
        ids=_ADD_EXACT_CAP_IDS,
    )
    def test_add_job_accepts_value_at_exact_cap(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        exact_value = "a" * cap
        job = svc.add_job(name="j", message="m", every_secs=3600, **{field_name: exact_value})
        assert getattr(job, field_name) == exact_value


# ── Update-job tests ──


class TestUpdateJobFieldValidation:
    """_update_job_locked rejects non-string and oversize truthy values."""

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _NEWLY_VALIDATED_FIELDS,
        ids=_FIELD_IDS,
    )
    def test_update_job_rejects_oversize(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="max length"):
            svc.update_job(job.id, **{field_name: oversize_value})
        # Job unchanged
        reloaded = svc.list_jobs()[0]
        assert getattr(reloaded, field_name) == getattr(job, field_name)

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _NEWLY_VALIDATED_FIELDS,
        ids=_FIELD_IDS,
    )
    def test_update_job_rejects_non_string(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="must be a string"):
            svc.update_job(job.id, **{field_name: non_string_value})
        # Job unchanged
        reloaded = svc.list_jobs()[0]
        assert getattr(reloaded, field_name) == getattr(job, field_name)

    @pytest.mark.parametrize(
        "field_name,cap,oversize_value,non_string_value",
        _UPDATE_ASSIGNED_FIELDS,
        ids=_UPDATE_ASSIGNED_IDS,
    )
    def test_update_job_accepts_value_at_exact_cap(
        self, tmp_path, field_name, cap, oversize_value, non_string_value
    ):
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        exact_value = "a" * cap
        updated = svc.update_job(job.id, **{field_name: exact_value})
        assert updated is not None
        assert getattr(updated, field_name) == exact_value

    def test_timezone_semantic_acceptance(self, tmp_path):
        """A real IANA timezone passes both the cap gate and is_valid_timezone.

        Uses one of the longest real zone names so the length gate is
        exercised with a value the semantic check also accepts.
        """
        tz = "America/Argentina/ComodRivadavia"
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600, timezone=tz)
        assert job.timezone == tz
        updated = svc.update_job(job.id, timezone=tz)
        assert updated is not None
        assert updated.timezone == tz


# ── Falsy-skip semantics ──


class TestFalsySkipSemantics:
    """Falsy values (None, '') are no-ops in update and valid defaults in create."""

    @pytest.mark.parametrize("field_name", _FIELD_IDS)
    def test_update_with_none_is_noop(self, tmp_path, field_name):
        """Passing None for a field in update does not raise."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        # Should not raise - falsy values are skipped
        updated = svc.update_job(job.id, **{field_name: None})
        assert updated is not None

    @pytest.mark.parametrize("field_name", _FIELD_IDS)
    def test_update_with_empty_string_is_noop(self, tmp_path, field_name):
        """Passing '' for a field in update does not raise."""
        svc = CronService(base_dir=tmp_path)
        job = svc.add_job(name="j", message="m", every_secs=3600)
        # Should not raise - falsy values are skipped
        updated = svc.update_job(job.id, **{field_name: ""})
        assert updated is not None

    @pytest.mark.parametrize("field_name", _FIELD_IDS)
    def test_create_with_none_accepted_for_optional_fields(self, tmp_path, field_name):
        """Optional fields accept None at create time (falsy skip)."""
        svc = CronService(base_dir=tmp_path)
        # None is the default for optional fields; should not raise
        job = svc.add_job(name="j", message="m", every_secs=3600, **{field_name: None})
        assert job is not None

    @pytest.mark.parametrize("falsy_non_string", [[], 0, False, {}], ids=repr)
    def test_falsy_non_strings_are_rejected_not_skipped(self, tmp_path, falsy_non_string):
        """The not-set skip admits exactly None and "" — no other falsy value.

        A bare truthiness test would silently persist a falsy non-string
        (e.g. channel=[]) verbatim into crons.json, the exact bypass this
        chokepoint exists to close.
        """
        svc = CronService(base_dir=tmp_path)
        with pytest.raises(ValueError, match="must be a string"):
            svc.add_job(name="j", message="m", every_secs=3600, channel=falsy_non_string)
        assert svc.list_jobs() == []
        job = svc.add_job(name="j", message="m", every_secs=3600)
        with pytest.raises(ValueError, match="must be a string"):
            svc.update_job(job.id, channel=falsy_non_string)
        assert svc.list_jobs()[0].channel is None


# ── Cap alignment: the table must not drift from the boundary schemas ──


class TestCapAlignment:
    """Pins each table cap to its boundary-schema counterpart.

    The table's caps are documented as "matching the REST/MCP boundary
    schemas". Without this pin the claim can silently stop being true when
    either side changes (the table entries are fresh literal copies of caps
    spelled in validation.py). Fields with no CRON_ADD_SCHEMA entry are
    pinned to MAX_SHORT_STRING, the general ID cap.
    """

    # table field -> CRON_ADD_SCHEMA field (where the names differ)
    _SCHEMA_NAME_MAP = {"agent_id": "agent"}
    # No boundary FieldSpec exists for these; the general ID cap applies.
    _NO_SCHEMA_FIELDS = {"created_by", "session_key", "folder_id"}

    def test_table_caps_match_cron_add_schema(self):
        schema_caps = {spec.name: spec.max_len for spec in CRON_ADD_SCHEMA.fields}
        for field_name, cap in _CRON_STRING_FIELD_CAPS:
            if field_name in self._NO_SCHEMA_FIELDS:
                assert cap == MAX_SHORT_STRING, (
                    f"{field_name} has no boundary schema entry and must use "
                    f"MAX_SHORT_STRING, got {cap}"
                )
                continue
            schema_name = self._SCHEMA_NAME_MAP.get(field_name, field_name)
            assert schema_name in schema_caps, (
                f"{field_name} (schema name {schema_name!r}) not found in "
                f"CRON_ADD_SCHEMA — update the mapping in this test"
            )
            assert cap == schema_caps[schema_name], (
                f"table cap for {field_name} ({cap}) drifted from "
                f"CRON_ADD_SCHEMA's {schema_name} max_len "
                f"({schema_caps[schema_name]})"
            )


# ── Anti-drift: every persisted string field must be validated ──


class TestAntiDrift:
    """Structural guard: a new string field cannot ship without validation.

    Inspects the CronJob dataclass to enumerate all str-typed fields, removes
    the known set of runtime-only / non-caller-supplied fields, and asserts that
    every remaining field appears in the validation table.
    """

    # Fields excluded from the anti-drift assertion:
    # - id: generated internally by uuid.uuid4().hex[:8], never caller-supplied
    # - last_status: set only by the execution engine ("ok" | "error")
    # - last_error: set only by the execution engine on failure
    # - last_result: set only by set_run_result() during execution
    # - last_result_stamp: the run stamp, rendered by set_run_result() beside
    #   last_result and never accepted from a caller, so there is no boundary
    #   schema for its cap to mirror
    # - last_posted_hash: set by dedup logic when a Slack post is delivered
    # - last_failure_hash: set by dedup logic when a failure notification fires
    # - approval_mode: validated by a separate finite-set check, not length
    _RUNTIME_ONLY_FIELDS: frozenset[str] = frozenset(
        {
            "id",
            "last_status",
            "last_error",
            "last_result",
            "last_result_stamp",
            "last_posted_hash",
            "last_failure_hash",
            "approval_mode",
        }
    )

    def test_all_persisted_string_fields_are_validated(self):
        """Every caller-supplied string field on CronJob has validation."""
        all_str_fields: set[str] = set()
        for f in dataclasses.fields(CronJob):
            # Accept str and str | None (Optional[str])
            annotation = f.type
            if annotation == "str" or annotation == "str | None":
                all_str_fields.add(f.name)

        # Remove runtime-only fields that are never caller-supplied
        persisted_caller_fields = all_str_fields - self._RUNTIME_ONLY_FIELDS

        missing = persisted_caller_fields - _VALIDATED_FIELD_NAMES
        assert missing == set(), (
            f"String field(s) {sorted(missing)} on CronJob are not covered by "
            f"_CRON_STRING_FIELD_CAPS. Add them to the validation table or to "
            f"the _RUNTIME_ONLY_FIELDS exclusion set with a comment explaining why."
        )

    def test_validation_table_field_names_are_valid_cronjob_fields(self):
        """Every name in _CRON_STRING_FIELD_CAPS maps to an actual CronJob field.

        Catches typos in the table that would silently skip validation for the
        intended field.
        """
        cronjob_field_names = {f.name for f in dataclasses.fields(CronJob)}
        table_field_names = {name for name, _ in _CRON_STRING_FIELD_CAPS}

        invalid = table_field_names - cronjob_field_names
        assert invalid == set(), (
            f"Validation table contains field name(s) {sorted(invalid)} that do "
            f"not exist on CronJob. Fix the typo in _CRON_STRING_FIELD_CAPS."
        )
