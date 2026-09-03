"""``resource_limits`` schema, its single parse site, and the traps it closes (#3474).

This block used to have no dataclass and no validation: four consumers each read
the raw dict and invented their own parse rule, so the two mechanisms that share
``max_processes`` / ``max_memory_mb`` disagreed about what ``0`` means with
nothing in the tree recording it. These tests pin the parse rule, pin that each
consumer's interpretation of ``0`` did NOT change, and pin the two inputs that
were live defects before the convergence.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew.config.loader import (
    _WARNED_RESOURCE_LIMIT_KEYS,
    KiroCrewConfig,
    ResourceLimitsConfig,
)

# Every key the block carries. A key missing here is a key the loader would drop
# on the next save, since an unrecognised key inside a KNOWN section is not
# round-tripped -- which is why this list is asserted against the dataclass.
_ALL_KEYS = (
    "max_open_files",
    "max_processes",
    "max_memory_mb",
    "max_cpu_seconds",
    "cpu_weight",
    "max_cpu_percent",
    "max_total_memory_mb",
    "max_total_processes",
    "xdist_auto_cap",
)


@pytest.fixture(autouse=True)
def _reset_warn_once():
    """The refusal log is once-per-key-per-process, so tests must not inherit it."""
    _WARNED_RESOURCE_LIMIT_KEYS.clear()
    yield
    _WARNED_RESOURCE_LIMIT_KEYS.clear()


def _load_from_dict(data: object) -> KiroCrewConfig:
    """Write *data* to a temp config file and load it through the real loader."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        tmp = Path(f.name)
    try:
        with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
            return KiroCrewConfig.load()
    finally:
        tmp.unlink(missing_ok=True)


class TestSchemaShape:
    def test_the_dataclass_covers_every_key_a_consumer_reads(self):
        import dataclasses

        assert {f.name for f in dataclasses.fields(ResourceLimitsConfig)} == set(_ALL_KEYS)

    def test_unset_is_none_and_not_zero(self):
        """``None`` and ``0`` cannot collapse: ``0`` is a real request on the
        rlimit path ("leave inherited") and must stay distinguishable from
        "operator said nothing"."""
        cfg = ResourceLimitsConfig()
        for key in _ALL_KEYS:
            assert getattr(cfg, key) is None, key

    def test_absent_section_loads_as_all_unset(self):
        cfg = _load_from_dict({})
        for key in _ALL_KEYS:
            assert getattr(cfg.resource_limits, key) is None, key

    def test_a_non_dict_section_degrades_instead_of_raising(self):
        for junk in (None, 5, "limits", [], True):
            parsed = ResourceLimitsConfig.from_raw(junk)
            assert parsed == ResourceLimitsConfig(), junk

    def test_the_section_round_trips_through_load_and_to_dict(self):
        cfg = _load_from_dict({"resource_limits": {"max_memory_mb": 4096, "cpu_weight": 80}})
        assert cfg.resource_limits.max_memory_mb == 4096
        assert cfg.resource_limits.cpu_weight == 80
        emitted = cfg.to_dict()["resource_limits"]
        assert emitted["max_memory_mb"] == 4096
        assert emitted["cpu_weight"] == 80
        # Every key is emitted, so a later reader sees the whole domain.
        assert set(emitted) == set(_ALL_KEYS)

    def test_a_fraction_survives_the_FULL_loader_path_and_truncates(self):
        """The truncation rule has to hold through ``KiroCrewConfig.load()``, not
        just through ``from_raw`` in isolation.

        ``config/validation.py`` runs jsonschema over the raw dict and POPS a
        type-mismatched value (``_apply_field_default``, depth-capped at two
        levels -- exactly ``resource_limits.<key>``) before the loader builds the
        section. Declaring these fields integer-only therefore deleted a
        hand-edited ``512.5`` before ``from_raw`` could truncate it, and because
        ``to_dict`` now owns this section a later save wrote ``null`` over it.
        Worse than doing nothing: on the rlimit path the fallback is ``0``, which
        means "leave inherited", so a 512 MB ceiling became NO ceiling.
        """
        cfg = _load_from_dict({"resource_limits": {"max_memory_mb": 512.5}})
        assert cfg.resource_limits.max_memory_mb == 512
        # And the normalized value is what a save persists -- not null.
        assert cfg.to_dict()["resource_limits"]["max_memory_mb"] == 512

    def test_a_refused_value_does_not_survive_the_full_loader_path(self):
        """The other direction: a value the parse rule refuses must still land on
        ``None`` after a full load, so the consumer default applies."""
        cfg = _load_from_dict({"resource_limits": {"max_processes": 0.5, "cpu_weight": 20000}})
        assert cfg.resource_limits.max_processes is None
        assert cfg.resource_limits.cpu_weight is None


class TestParseRule:
    def test_explicit_zero_is_kept(self):
        """The rlimit path documents ``0`` as "leave the limit unchanged", so the
        parse must pass it through rather than treat it as absent."""
        parsed = ResourceLimitsConfig.from_raw({"max_open_files": 0, "max_processes": 0})
        assert parsed.max_open_files == 0
        assert parsed.max_processes == 0

    def test_a_fraction_that_would_truncate_to_zero_is_refused(self):
        """#3474's trap. ``int(0.5) == 0``, and ``0`` already MEANS something on
        both paths -- "leave inherited" on one, "use the default" on the other --
        so truncating would silently reinterpret the operator's value."""
        parsed = ResourceLimitsConfig.from_raw({"max_processes": 0.5, "max_memory_mb": 0.25})
        assert parsed.max_processes is None
        assert parsed.max_memory_mb is None

    def test_a_fraction_above_one_truncates_toward_zero(self):
        """Every reader this replaces truncated, and truncation can only make a
        ceiling stricter -- so tightening the parse must not loosen a limit."""
        parsed = ResourceLimitsConfig.from_raw({"max_memory_mb": 512.5, "max_processes": 8.9})
        assert parsed.max_memory_mb == 512
        assert parsed.max_processes == 8

    @pytest.mark.parametrize("key", _ALL_KEYS)
    def test_a_negative_fraction_never_truncates_into_range(self, key):
        """The range check runs on the value AS WRITTEN. Checking the truncated
        result instead let ``-0.5`` through as ``0`` on every floor-at-zero key,
        which on the rlimit path reads as "leave inherited" and removes the
        ceiling the operator was setting -- weaker than the value they wrote and
        weaker than the default they would have got by writing nothing."""
        assert getattr(ResourceLimitsConfig.from_raw({key: -0.5}), key) is None
        assert getattr(ResourceLimitsConfig.from_raw({key: -0.9}), key) is None

    def test_a_negative_fraction_does_not_disable_the_nofile_ceiling(self):
        """The reported shape, end to end: before the fix ``-0.5`` reached
        ``resource_limit_spec`` as ``0``, and ``0`` is dropped there, so the
        process ran with no RLIMIT_NOFILE at all."""
        from kiro_crew.security import _RLIMIT_DEFAULTS, resource_limit_spec

        spec = dict(resource_limit_spec({"resource_limits": {"max_open_files": -0.5}}))
        assert spec["RLIMIT_NOFILE"] == _RLIMIT_DEFAULTS["max_open_files"]

    def test_a_fraction_below_the_xdist_floor_is_refused(self):
        """``xdist_auto_cap`` floors at -1, so -0.5 is IN range but still
        truncates onto 0, which means "disabled" there rather than "auto"."""
        assert ResourceLimitsConfig.from_raw({"xdist_auto_cap": -0.5}).xdist_auto_cap is None
        assert ResourceLimitsConfig.from_raw({"xdist_auto_cap": -1.5}).xdist_auto_cap is None

    def test_an_integral_float_is_accepted(self):
        assert ResourceLimitsConfig.from_raw({"max_memory_mb": 2048.0}).max_memory_mb == 2048

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_nan_and_infinity_are_refused_before_int_sees_them(self, bad):
        """``json.loads`` accepts the ``NaN`` / ``Infinity`` literals, and
        ``int(inf)`` raises OverflowError."""
        parsed = ResourceLimitsConfig.from_raw(dict.fromkeys(_ALL_KEYS, bad))
        for key in _ALL_KEYS:
            assert getattr(parsed, key) is None, key

    def test_a_bool_is_not_a_number(self):
        """``True`` would coerce to ``1``: a one-task / one-MB ceiling that kills
        the process it is supposed to bound."""
        parsed = ResourceLimitsConfig.from_raw({"max_processes": True, "max_memory_mb": False})
        assert parsed.max_processes is None
        assert parsed.max_memory_mb is None

    def test_a_string_is_refused(self):
        assert ResourceLimitsConfig.from_raw({"max_processes": "200"}).max_processes is None

    def test_out_of_range_is_refused_rather_than_clamped(self):
        """Clamping would move a confinement ceiling away from the number the
        operator can read in their own file; the readers this replaces all fell
        back to the documented default instead."""
        assert ResourceLimitsConfig.from_raw({"cpu_weight": 20000}).cpu_weight is None
        assert ResourceLimitsConfig.from_raw({"cpu_weight": 0}).cpu_weight is None
        assert ResourceLimitsConfig.from_raw({"cpu_weight": 10000}).cpu_weight == 10000
        assert ResourceLimitsConfig.from_raw({"max_processes": -1}).max_processes is None

    def test_xdist_auto_cap_keeps_its_minus_one_sentinel(self):
        assert ResourceLimitsConfig.from_raw({"xdist_auto_cap": -1}).xdist_auto_cap == -1
        assert ResourceLimitsConfig.from_raw({"xdist_auto_cap": 0}).xdist_auto_cap == 0
        assert ResourceLimitsConfig.from_raw({"xdist_auto_cap": -2}).xdist_auto_cap is None

    def test_a_refusal_is_logged_once_per_key(self, caplog):
        """Silence is what made this class of typo undiagnosable: the operator
        saw a systemd range error, or nothing at all."""
        with caplog.at_level("WARNING", logger="kiro_crew.config.loader"):
            for _ in range(3):
                ResourceLimitsConfig.from_raw({"max_processes": 0.5})
        hits = [r for r in caplog.records if "resource_limits.max_processes" in r.getMessage()]
        assert len(hits) == 1, [r.getMessage() for r in hits]
        assert "0.5" in hits[0].getMessage()


class TestCgroupConsumerUnchanged:
    """The cgroup path treats 0/absent/junk as "use the module default" and must
    keep doing exactly that -- systemd rejects a zero property outright."""

    @staticmethod
    def _limits(raw: dict | None):
        import kiro_crew.sandbox as sb

        with unittest.mock.patch(
            "kiro_crew.config.loader._raw_config",
            return_value={} if raw is None else {"resource_limits": raw},
        ):
            return sb._cgroup_limits_from_config()

    def test_configured_values_are_honoured(self):
        procs, mem, weight, quota = self._limits(
            {"max_processes": 200, "max_memory_mb": 2048, "cpu_weight": 80, "max_cpu_percent": 400}
        )
        assert (procs, mem, weight, quota) == (200, 2048, 80, 400)

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            {},
            {"max_processes": 0, "max_memory_mb": 0, "cpu_weight": 0},
            {"max_processes": 0.5, "max_memory_mb": 0.5},
            {"max_processes": float("inf"), "max_memory_mb": float("nan")},
            {"max_processes": "x", "max_memory_mb": [1], "cpu_weight": True},
            {"max_processes": -5, "max_memory_mb": -5},
        ],
    )
    def test_everything_out_of_domain_lands_on_the_module_default(self, raw):
        import kiro_crew.sandbox as sb

        procs, mem, weight, quota = self._limits(raw)
        assert procs == sb._CGROUP_DEFAULT_MAX_PROCESSES
        assert mem == sb._default_max_memory_mb()
        assert weight == sb._CGROUP_DEFAULT_CPU_WEIGHT
        assert quota == 0  # opt-in: no CPUQuota emitted

    def test_the_scope_argv_never_carries_a_zero_ceiling(self):
        """The original outage: ``TasksMax=0`` / ``MemoryMax=0M`` are rejected by
        systemd, ``cgroup_scope_argv`` has no unwrapped-retry fallback, and both
        call sites spawn the argv as-is -- so a zero here fails every spawn."""
        import kiro_crew.sandbox as sb

        hostile = [
            {"max_processes": 0.5, "max_memory_mb": 0.5},
            {"max_processes": 0, "max_memory_mb": 0},
            {"max_processes": float("inf"), "max_memory_mb": float("inf")},
            {"max_processes": float("nan"), "max_memory_mb": float("nan")},
            {"max_processes": True, "max_memory_mb": True},
            {"max_processes": -1, "max_memory_mb": -1},
            {"max_processes": "0.5", "max_memory_mb": None},
        ]
        for raw in hostile:
            with (
                unittest.mock.patch.object(sb, "_probe_cgroup_scope", return_value=(True, "")),
                unittest.mock.patch.object(sb, "_reconcile_slice_memory_high_off_thread"),
                unittest.mock.patch.object(sb, "_cpu_controller_delegated", return_value=True),
                # #2602 pins the wrapper through ``trusted_system_bin`` before
                # wrapping; an unresolvable systemd-run (e.g. Windows CI) now
                # degrades to an unwrapped argv with no ceilings at all. Pin the
                # resolution so this test keeps exercising the ceiling-emitting
                # path on every platform, same as the probe mock above.
                unittest.mock.patch(
                    "kiro_crew.platform_compat.trusted_system_bin",
                    return_value="/usr/bin/systemd-run",
                ),
                unittest.mock.patch(
                    "kiro_crew.config.loader._raw_config",
                    return_value={"resource_limits": raw},
                ),
            ):
                argv = sb.cgroup_scope_argv(["/bin/true"])
            assert "TasksMax=0" not in argv, raw
            assert "MemoryMax=0M" not in argv, raw
            assert "CPUQuota=0%" not in argv, raw
            # A ceiling is still present -- degrading must not mean unbounded.
            tasks = [a for a in argv if a.startswith("TasksMax=")]
            memory = [a for a in argv if a.startswith("MemoryMax=")]
            assert tasks and int(tasks[0].split("=")[1]) >= 1, raw
            assert memory and int(memory[0].split("=")[1].rstrip("M")) >= 1, raw

    def test_an_unresolvable_wrapper_degrades_to_unwrapped_not_to_a_zero_ceiling(self):
        """Negative control for the wrapper-pin mock above: when the
        ``systemd-run`` pin cannot resolve (the reality on Windows, where #7183
        turned this file red), ``cgroup_scope_argv`` returns argv UNCHANGED --
        its documented fail-open, pinned in depth by
        ``test_sandbox_argv.py::test_cgroup_wrapper_is_absolute_or_refused``.
        Asserting it here too keeps the ceiling test above honest: the
        "degrading must not mean unbounded" invariant is about the emitted
        limit VALUES, and this is the shape degradation takes -- no properties
        at all, never a zero ceiling."""
        import kiro_crew.sandbox as sb

        try:
            with (
                unittest.mock.patch.object(sb, "_probe_cgroup_scope", return_value=(True, "")),
                unittest.mock.patch.object(sb, "_reconcile_slice_memory_high_off_thread"),
                unittest.mock.patch.object(sb, "_cpu_controller_delegated", return_value=True),
                unittest.mock.patch.object(
                    sb.platform_compat, "trusted_system_bin", return_value=None
                ),
                # Deliberately never read on the healthy path -- the wrapper
                # gate returns before ``_cgroup_limits_from_config`` runs. It
                # stays mocked so a REGRESSION of the fail-open (proceeding
                # past the gate) still cannot read the host's real config.
                unittest.mock.patch(
                    "kiro_crew.config.loader._raw_config",
                    return_value={"resource_limits": {"max_processes": 0.5, "max_memory_mb": 0.5}},
                ),
            ):
                argv = sb.cgroup_scope_argv(["/bin/true"])
        finally:
            # The degraded path arms the once-per-process warning latch; leaving
            # it set would silence the SECURITY warning for later tests on this
            # worker (same reset as test_sandbox_argv.py's _reset_probe).
            sb._CGROUP_WARNED = False
        assert argv == ["/bin/true"]
        # Drift guard for the name's claim: degradation means NO properties,
        # never a zero one.
        assert not [a for a in argv if a.startswith(("TasksMax=", "MemoryMax=", "CPUQuota="))]


class TestSliceConsumer:
    @staticmethod
    def _limits(raw: dict):
        import kiro_crew.sandbox as sb

        with unittest.mock.patch(
            "kiro_crew.config.loader._raw_config", return_value={"resource_limits": raw}
        ):
            return sb._slice_limits_from_config()

    def test_configured_values_are_honoured(self):
        assert self._limits({"max_total_memory_mb": 4096, "max_total_processes": 1000}) == (
            4096,
            1000,
        )

    def test_a_junk_memory_value_no_longer_discards_a_valid_process_ceiling(self):
        """Before the convergence this function tested ``int(m) >= 1`` directly.
        ``int(nan)`` raises, the raise landed in the function's own except, and
        BOTH fields fell back -- silently dropping an aggregate task ceiling the
        operator had set correctly, because a different key was malformed."""
        import kiro_crew.sandbox as sb

        mem, tasks = self._limits(
            {"max_total_memory_mb": float("nan"), "max_total_processes": 1000}
        )
        assert tasks == 1000
        assert mem == sb._default_max_total_memory_mb()

    def test_zero_and_junk_still_mean_use_the_default(self):
        import kiro_crew.sandbox as sb

        for raw in (
            {"max_total_memory_mb": 0, "max_total_processes": 0},
            {"max_total_processes": "x"},
        ):
            mem, tasks = self._limits(raw)
            assert mem == sb._default_max_total_memory_mb()
            assert tasks == sb._CGROUP_DEFAULT_MAX_TOTAL_TASKS


class TestRlimitConsumer:
    """The rlimit path is where ``0`` means "leave inherited". That documented
    behaviour has existing configs behind it and must survive the convergence."""

    def test_an_explicit_zero_still_disables_a_limit(self):
        from kiro_crew.security import resource_limit_spec

        spec = dict(resource_limit_spec({"resource_limits": {"max_open_files": 0}}))
        assert "RLIMIT_NOFILE" not in spec

    def test_defaults_apply_when_the_section_is_absent(self):
        from kiro_crew.security import _RLIMIT_DEFAULTS, resource_limit_spec

        spec = dict(resource_limit_spec({}))
        assert spec["RLIMIT_NOFILE"] == _RLIMIT_DEFAULTS["max_open_files"]
        # The three that default to 0 are dropped, not set to 0.
        assert "RLIMIT_NPROC" not in spec
        assert "RLIMIT_CPU" not in spec
        assert "RLIMIT_AS" not in spec

    def test_configured_values_are_honoured(self):
        from kiro_crew.security import resource_limit_spec

        spec = dict(
            resource_limit_spec({"resource_limits": {"max_processes": 64, "max_memory_mb": 512}})
        )
        assert spec["RLIMIT_NPROC"] == 64
        assert spec["RLIMIT_AS"] == 512 * 1024 * 1024

    @pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
    def test_a_non_finite_value_no_longer_raises(self, bad):
        """``inf >= 0`` passed the old guard and ``int(inf)`` then raised
        OverflowError. Nothing on this path catches it: it propagated out of
        ``sandbox.resource_limit_preexec``, which builds the preexec_fn for every
        routed spawn, so one typo in config.json failed every agent launch."""
        from kiro_crew.security import _RLIMIT_DEFAULTS, resource_limit_spec

        spec = dict(resource_limit_spec({"resource_limits": {"max_processes": bad}}))
        assert "RLIMIT_NPROC" not in spec
        # The unrelated keys are untouched by one bad neighbour.
        assert spec["RLIMIT_NOFILE"] == _RLIMIT_DEFAULTS["max_open_files"]

    def test_a_fraction_falls_back_to_the_default_instead_of_disabling(self):
        """``int(0.5) == 0`` is this path's "leave inherited" sentinel, so a
        fractional request used to silently remove the ceiling the operator was
        asking for. It now lands on the documented default."""
        from kiro_crew.security import _RLIMIT_DEFAULTS, resource_limit_spec

        spec = dict(resource_limit_spec({"resource_limits": {"max_open_files": 0.5}}))
        assert spec["RLIMIT_NOFILE"] == _RLIMIT_DEFAULTS["max_open_files"]


class TestXdistConsumer:
    @staticmethod
    def _cap(raw: dict):
        import kiro_crew.resource_status as rs

        with unittest.mock.patch(
            "kiro_crew.config.loader._raw_config", return_value={"resource_limits": raw}
        ):
            return rs._xdist_cap_config()

    def test_sentinels_and_junk(self):
        assert self._cap({"xdist_auto_cap": 6}) == 6
        assert self._cap({"xdist_auto_cap": 0}) == 0
        assert self._cap({"xdist_auto_cap": -1}) == -1
        assert self._cap({}) == -1
        assert self._cap({"xdist_auto_cap": float("inf")}) == -1
        assert self._cap({"xdist_auto_cap": -2}) == -1


class TestOverlayOwnership:
    """``config.local.json`` values must not leak into the base ``config.json``.

    ``save()`` strips overlay-owned leaves via ``_subtract_overlay``, which
    matches on EQUAL VALUES. Making ``resource_limits`` a known section put it in
    reach of that comparison for the first time: while it was an unknown section
    it round-tripped verbatim through ``_extra_sections``, so an overlay value
    always compared equal to itself. Now the loader normalizes it, and a
    normalized value does not equal the raw one it came from -- so the subtraction
    stops recognising it as overlay-owned and copies it into the base file.
    """

    @staticmethod
    def _save_with_overlay(tmp_path: Path, base: dict, overlay: dict) -> dict:
        cfg_file = tmp_path / "config.json"
        local = tmp_path / "config.local.json"
        cfg_file.write_text(json.dumps(base), encoding="utf-8")
        local.write_text(json.dumps(overlay), encoding="utf-8")
        with (
            unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            unittest.mock.patch("kiro_crew.config.loader.config_local_path", return_value=local),
        ):
            KiroCrewConfig.load().save()
        return json.loads(cfg_file.read_text(encoding="utf-8"))

    def test_an_integer_overlay_limit_stays_out_of_the_base_config(self, tmp_path: Path):
        written = self._save_with_overlay(
            tmp_path, {"agent": {"model": "m"}}, {"resource_limits": {"max_memory_mb": 4096}}
        )
        assert "max_memory_mb" not in written.get("resource_limits", {})

    def test_a_NORMALIZED_overlay_limit_stays_out_of_the_base_config(self, tmp_path: Path):
        """The reported shape: a fractional overlay value is normalized to 512 on
        load, so a raw-value comparison no longer matches 512.5 and the override
        was persisted into config.json -- the exact leak the subtraction exists to
        prevent."""
        written = self._save_with_overlay(
            tmp_path, {"agent": {"model": "m"}}, {"resource_limits": {"max_memory_mb": 512.5}}
        )
        assert "max_memory_mb" not in written.get("resource_limits", {}), written.get(
            "resource_limits"
        )

    def test_a_REFUSED_overlay_limit_stays_out_of_the_base_config(self, tmp_path: Path):
        """Same hazard from the other side: a refused value becomes None, and a
        null must not be written into the base file either."""
        written = self._save_with_overlay(
            tmp_path, {"agent": {"model": "m"}}, {"resource_limits": {"max_processes": 0.5}}
        )
        assert "max_processes" not in written.get("resource_limits", {}), written.get(
            "resource_limits"
        )

    def test_a_base_owned_limit_is_still_written(self, tmp_path: Path):
        """The subtraction must not over-reach: a value that is NOT in the overlay
        has to survive the save."""
        written = self._save_with_overlay(
            tmp_path,
            {"resource_limits": {"max_memory_mb": 2048}},
            {"agent": {"model": "m"}},
        )
        assert written["resource_limits"]["max_memory_mb"] == 2048


class TestSingleParseSite:
    """Drift guard. The defect in #3474 was not any one parse rule -- it was that
    there were four of them, so a contributor tightening one could not see the
    others. A fifth reader must fail this test rather than be discovered later."""

    def test_no_module_outside_the_section_parser_parses_these_keys_itself(self):
        src = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        allowed = src / "config" / "sections.py"
        # A raw ``.get("<key>")`` is how each of the retired readers pulled its
        # value straight out of the config dict.
        pattern = re.compile(r"\.get\(\s*[\"'](" + "|".join(_ALL_KEYS) + r")[\"']")
        offenders: list[str] = []
        for path in src.rglob("*.py"):
            if path == allowed:
                continue
            for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(src)}:{num}: {line.strip()}")
        assert offenders == [], (
            "resource_limits keys must be parsed only by "
            "ResourceLimitsConfig.from_raw in config/sections.py -- these sites read "
            "the raw dict themselves, which is how the two mechanisms' "
            "incompatible meanings of 0 drifted apart:\n" + "\n".join(offenders)
        )

    def test_the_consumers_go_through_the_validated_object(self):
        """The other half of the guard: the readers must still be READING the
        block, so a refactor cannot satisfy the test above by dropping config
        support altogether."""
        src = Path(__file__).resolve().parent.parent / "src" / "kiro_crew"
        for rel in ("sandbox.py", "security.py", "resource_status.py"):
            body = (src / rel).read_text(encoding="utf-8")
            assert "ResourceLimitsConfig" in body, rel
