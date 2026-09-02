"""Property tests for manifest hooks and extended CronEntry.

Feature: app-sdk-gateway-hooks
Properties 12, 13: Hook path validation and manifest round-trip.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_crew.apps.manifest import (
    AppManifest,
    BackendConfig,
    CronEntry,
    HooksConfig,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def _identifier() -> st.SearchStrategy[str]:
    """Generate valid Python identifiers."""
    return st.from_regex(r"[a-z][a-z0-9_]{0,10}", fullmatch=True)


def _dotted_path() -> st.SearchStrategy[str]:
    """Generate valid dotted module paths like 'backend.routes'."""
    return st.lists(_identifier(), min_size=1, max_size=4).map(lambda parts: ".".join(parts))


def _hook_path() -> st.SearchStrategy[str]:
    """Generate valid hook paths like 'backend.routes:register_routes'."""
    return st.tuples(_dotted_path(), _identifier()).map(lambda t: f"{t[0]}:{t[1]}")


def _hooks_config() -> st.SearchStrategy[HooksConfig]:
    """Generate HooksConfig with optional valid hook paths."""
    return st.builds(
        HooksConfig,
        routes=st.one_of(st.just(""), _hook_path()),
        on_startup=st.one_of(st.just(""), _hook_path()),
        on_shutdown=st.one_of(st.just(""), _hook_path()),
    )


def _env_dict() -> st.SearchStrategy[dict[str, str]]:
    """Generate environment variable dicts."""
    key = st.from_regex(r"[A-Z][A-Z0-9_]{0,10}", fullmatch=True)
    val = st.text(min_size=0, max_size=20, alphabet=st.characters(whitelist_categories=("L", "N", "P")))
    return st.dictionaries(key, val, max_size=3)


def _cron_entry() -> st.SearchStrategy[CronEntry]:
    """Generate CronEntry with extended fields."""
    return st.builds(
        CronEntry,
        name=st.from_regex(r"[a-z][a-z0-9-]{0,15}", fullmatch=True),
        every=st.integers(min_value=0, max_value=86400),
        cron_expr=st.one_of(st.just(""), st.just("* * * * *"), st.just("0 */6 * * *")),
        agent=st.one_of(st.just(""), st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True)),
        message=st.text(min_size=0, max_size=50, alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z"))),
        agent_sequence=st.lists(st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True), max_size=3),
        env=_env_dict(),
        persistent_session=st.booleans(),
        silent=st.booleans(),
        timezone=st.sampled_from(["", "UTC", "America/New_York", "Australia/Sydney"]),
        skip_dates=st.lists(
            st.sampled_from(["2026-01-01", "2026-12-25", "2027-07-04"]), max_size=3
        ),
    )


# ---------------------------------------------------------------------------
# Property 13: Manifest round-trip with hooks and extended crons
# ---------------------------------------------------------------------------


class TestManifestRoundTrip:
    """Property 13: Manifest round-trip with hooks and extended crons.

    **Validates: Requirements 6.4, 6.5**
    """

    @settings(max_examples=100)
    @given(hooks=_hooks_config(), crons=st.lists(_cron_entry(), min_size=0, max_size=3))
    def test_hooks_round_trip(self, hooks: HooksConfig, crons: list[CronEntry]) -> None:
        """For any valid manifest with hooks and extended crons,
        serializing then deserializing produces an equivalent manifest."""
        manifest = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test App",
            description="A test app",
            backend=BackendConfig(hooks=hooks),
            crons=crons,
        )
        serialized = manifest.to_dict()
        restored = AppManifest.from_dict(serialized)

        # Hooks round-trip
        assert restored.backend.hooks.routes == hooks.routes
        assert restored.backend.hooks.on_startup == hooks.on_startup
        assert restored.backend.hooks.on_shutdown == hooks.on_shutdown

        # Cron extended fields round-trip
        assert len(restored.crons) == len(crons)
        for orig, rest in zip(crons, restored.crons):
            assert rest.name == orig.name
            assert rest.every == orig.every
            assert rest.cron_expr == orig.cron_expr
            assert rest.agent == orig.agent
            assert rest.message == orig.message
            assert rest.agent_sequence == orig.agent_sequence
            assert rest.env == orig.env
            assert rest.persistent_session == orig.persistent_session
            assert rest.silent == orig.silent
            assert rest.timezone == orig.timezone
            assert rest.skip_dates == orig.skip_dates

    @settings(max_examples=50)
    @given(hooks=_hooks_config())
    def test_hooks_config_round_trip_isolated(self, hooks: HooksConfig) -> None:
        """HooksConfig round-trips through to_dict/from_dict."""
        d = hooks.to_dict()
        restored = HooksConfig.from_dict(d)
        assert restored.routes == hooks.routes
        assert restored.on_startup == hooks.on_startup
        assert restored.on_shutdown == hooks.on_shutdown

    @settings(max_examples=50)
    @given(cron=_cron_entry())
    def test_cron_entry_round_trip(self, cron: CronEntry) -> None:
        """CronEntry round-trips through to_dict/from_dict."""
        d = cron.to_dict()
        restored = CronEntry.from_dict(d)
        assert restored.name == cron.name
        assert restored.every == cron.every
        assert restored.cron_expr == cron.cron_expr
        assert restored.agent == cron.agent
        assert restored.message == cron.message
        assert restored.agent_sequence == cron.agent_sequence
        assert restored.env == cron.env
        assert restored.persistent_session == cron.persistent_session
        assert restored.silent == cron.silent
        assert restored.timezone == cron.timezone
        assert restored.skip_dates == cron.skip_dates

    def test_to_dict_omits_empty_calendar_fields(self) -> None:
        """An entry that names no zone or skip dates serializes exactly as before.

        ``AppManifest.signing_payload`` folds each entry's ``to_dict()`` into the
        signed body, so emitting ``timezone``/``skip_dates`` unconditionally
        would change the payload of every already-signed manifest and invalidate
        its signature. Both keys are therefore present only when set.
        """
        d = CronEntry(name="refresh", every=600, message="go").to_dict()
        assert "timezone" not in d
        assert "skip_dates" not in d

    def test_to_dict_emits_calendar_fields_when_set(self) -> None:
        """A zone or skip list the publisher declared reaches the signed payload."""
        d = CronEntry(
            name="market-open",
            cron_expr="30 9 * * 1-5",
            timezone="America/New_York",
            skip_dates=["2026-12-25"],
        ).to_dict()
        assert d["timezone"] == "America/New_York"
        assert d["skip_dates"] == ["2026-12-25"]

    def test_from_dict_coerces_null_string_fields_to_empty(self) -> None:
        """Explicit JSON null on a string field deserializes to "" not "None".

        Regression anchor for the ``_str_or_empty`` helper: a malformed
        app.json with ``"name": null`` (or null on any string-typed cron
        field) must coerce to the empty string. The prior
        ``str(data.get(...))`` form turned ``None`` into the literal string
        ``"None"``, which would then be treated as a real value downstream.
        """
        entry = CronEntry.from_dict(
            {
                "name": None,
                "cron_expr": None,
                "agent": None,
                "message": None,
                "command": None,
                "script": None,
                "every": 60,
            }
        )
        assert entry.name == ""
        assert entry.cron_expr == ""
        assert entry.agent == ""
        assert entry.message == ""
        assert entry.command == ""
        assert entry.script == ""
        # Non-string fields keep their normal coercion / defaults.
        assert entry.every == 60

    def test_from_dict_coerces_null_container_fields_to_empty(self) -> None:
        """Explicit JSON null on a list/dict/number field must not raise.

        ``data.get(key, [])`` defends only the ABSENT key. An app.json that
        writes ``"skip_dates": null`` (hand-edited, or emitted by a generator)
        returns ``None``, and the comprehension over it raised ``TypeError``
        straight out of ``from_dict`` -- reached from ``/api/apps/register``,
        that is an HTTP 500 rather than a validation error. Same for
        ``"env": null`` (``AttributeError`` on ``.items()``) and
        ``"every": null`` (``TypeError`` in ``int()``).
        """
        entry = CronEntry.from_dict(
            {
                "name": "daily",
                "cron_expr": "0 6 * * *",
                "every": None,
                "agent_sequence": None,
                "env": None,
                "skip_dates": None,
            }
        )
        assert entry.every == 0
        assert entry.agent_sequence == []
        assert entry.env == {}
        assert entry.skip_dates == []

    @pytest.mark.parametrize("bogus", [5, "0 6 * * *", {"a": 1}, True])
    def test_from_dict_reports_wrong_typed_containers_instead_of_erasing(
        self, bogus: Any
    ) -> None:
        """A scalar/object where a list belongs degrades to empty AND is REPORTED.

        Degrading is required (the register path must not 500), but degrading
        SILENTLY is its own bug: an author who wrote
        ``"skip_dates": "2026-12-25"`` -- a bare string, not an array -- asked
        for a skip, and dropping it without a word lets the job fire on the
        excluded date. So the violation is recorded on the entry and surfaces as
        a validation error, the same shape as ``enabled_type_invalid``. (Before
        the type gate this string spelled ten one-character skip dates, which
        validation happened to reject -- loud, but for the wrong reason.)
        """
        entry = CronEntry.from_dict(
            {
                "name": "daily",
                "cron_expr": "0 6 * * *",
                "agent_sequence": bogus,
                "skip_dates": bogus,
            }
        )
        assert entry.agent_sequence == []
        assert entry.skip_dates == []
        assert set(entry.type_invalid_fields) == {"agent_sequence", "skip_dates"}

        errors = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="Test",
            crons=[entry],
        ).validate()
        assert any("skip_dates" in e and "wrong JSON type" in e for e in errors)
        assert any("agent_sequence" in e and "wrong JSON type" in e for e in errors)

    def test_from_dict_reports_a_wrong_typed_env_and_every(self) -> None:
        """The dict and numeric readers report their own violations too."""
        entry = CronEntry.from_dict(
            {"name": "daily", "cron_expr": "0 6 * * *", "env": ["A=1"], "every": "soon"}
        )
        assert entry.env == {}
        assert entry.every == 0
        assert set(entry.type_invalid_fields) == {"env", "every"}

    def test_from_dict_flags_a_boolean_every(self) -> None:
        """``"every": true`` is a type slip, not a 1-second schedule.

        ``bool`` is an ``int`` subclass, so an isinstance check alone would let
        ``True`` through as ``int(True) == 1`` -- the fastest possible interval,
        from a manifest that meant nothing of the kind.
        """
        entry = CronEntry.from_dict({"name": "daily", "cron_expr": "0 6 * * *", "every": True})
        assert entry.every == 0
        assert entry.type_invalid_fields == ["every"]

    def test_from_dict_reports_a_wrong_typed_timezone(self) -> None:
        """A non-string zone is reported, not quietly dropped to the fallback.

        Discarding it silently reproduces the bug the field exists to fix:
        validation would pass, the job would persist ``timezone=""`` and fire in
        the fallback zone (UTC on a fresh install), and nothing would say why the
        schedule ran on the wrong calendar day. That is why ``timezone`` is
        recorded while a plain string field like ``agent`` is not -- a dropped
        agent degrades visibly, a dropped zone does not.
        """
        entry = CronEntry.from_dict(
            {"name": "daily", "cron_expr": "0 6 * * *", "timezone": ["America/New_York"]}
        )
        assert entry.timezone == ""
        assert entry.type_invalid_fields == ["timezone"]

        errors = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="Test",
            crons=[entry],
        ).validate()
        assert any("timezone" in e and "wrong JSON type" in e for e in errors)

    def test_null_timezone_is_not_reported(self) -> None:
        """Null stays "not set": no error, and the config-then-UTC fallback."""
        entry = CronEntry.from_dict(
            {"name": "daily", "cron_expr": "0 6 * * *", "timezone": None}
        )
        assert entry.timezone == ""
        assert entry.type_invalid_fields == []

    def test_from_dict_survives_a_json_infinity_every(self) -> None:
        """``"every": 1e1000000`` parses to float('inf'), which int() refuses.

        This is reachable from real JSON, not a synthetic value: ``json.loads``
        accepts an out-of-range float literal and yields ``inf``, so the literal
        below is what an app.json can legally contain. Unguarded it raised
        ``OverflowError`` out of ``from_dict`` -- an HTTP 500 on
        ``/api/apps/register``, the same failure shape as the null case.
        """
        data = json.loads('{"name": "daily", "cron_expr": "0 6 * * *", "every": 1e1000000}')
        assert data["every"] == float("inf")  # the literal really does parse to inf

        entry = CronEntry.from_dict(data)
        assert entry.every == 0
        assert entry.type_invalid_fields == ["every"]

    def test_from_dict_survives_negative_infinity_and_nan_every(self) -> None:
        for value in (float("-inf"), float("nan")):
            entry = CronEntry.from_dict(
                {"name": "daily", "cron_expr": "0 6 * * *", "every": value}
            )
            assert entry.every == 0
            assert entry.type_invalid_fields == ["every"]

    def test_null_container_fields_are_not_reported_as_type_violations(self) -> None:
        """JSON null means "not set", the spelling a generator emits for absent.

        It must not raise (see above) and must not produce a validation error
        either, mirroring how ``_str_or_empty`` treats a null string field.
        """
        entry = CronEntry.from_dict(
            {
                "name": "daily",
                "cron_expr": "0 6 * * *",
                "every": None,
                "agent_sequence": None,
                "env": None,
                "skip_dates": None,
            }
        )
        assert entry.type_invalid_fields == []


# ---------------------------------------------------------------------------
# Property 12: Hook path validation
# ---------------------------------------------------------------------------


class TestHookPathValidation:
    """Property 12: Hook path validation.

    **Validates: Requirements 6.2, 6.3**
    """

    @settings(max_examples=100)
    @given(path=_hook_path())
    def test_valid_hook_paths_accepted(self, path: str) -> None:
        """Valid hook paths (module.path:callable_name) are accepted."""
        hooks = HooksConfig(routes=path)
        errors = hooks.validate()
        assert not errors, f"Valid path {path!r} rejected: {errors}"

    @pytest.mark.parametrize("invalid_path", [
        "no_colon_here",
        ":just_callable",
        "module:",
        "module..double:func",
        "123starts_with_num:func",
        "module:123func",
        "has space:func",
        "module:has space",
        "/absolute/path:func",
        "module.path:func:extra",
    ])
    def test_invalid_hook_paths_rejected(self, invalid_path: str) -> None:
        """Invalid hook paths are rejected with descriptive errors."""
        hooks = HooksConfig(routes=invalid_path)
        errors = hooks.validate()
        assert errors, f"Invalid path {invalid_path!r} was accepted"
        assert "backend.hooks.routes" in errors[0]

    @settings(max_examples=50)
    @given(
        routes=st.one_of(st.just(""), _hook_path()),
        on_startup=st.one_of(st.just(""), _hook_path()),
        on_shutdown=st.one_of(st.just(""), _hook_path()),
    )
    def test_empty_paths_always_valid(self, routes: str, on_startup: str, on_shutdown: str) -> None:
        """Empty hook paths are always valid (hooks are optional)."""
        hooks = HooksConfig(routes=routes, on_startup=on_startup, on_shutdown=on_shutdown)
        errors = hooks.validate()
        # Only non-empty paths can produce errors
        for err in errors:
            assert "got: ''" not in err

    def test_manifest_validation_includes_hooks(self) -> None:
        """AppManifest.validate() includes hook validation errors."""
        manifest = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="Test",
            backend=BackendConfig(hooks=HooksConfig(routes="invalid path")),
        )
        errors = manifest.validate()
        assert any("backend.hooks.routes" in e for e in errors)


class TestCronCalendarFieldValidation:
    """``timezone``/``skip_dates`` on a manifest cron are validated at validate().

    ``register_app_crons_with_service`` catches a per-job ``ValueError`` from the
    persistence owner, logs it and moves on -- so a bad zone shipped in an
    app.json would otherwise register NOTHING for that cron and say so only in
    the gateway log, leaving the author with a job that silently does not exist.
    Reporting it as a manifest validation error surfaces it at install time.
    """

    def _manifest(self, cron: CronEntry) -> AppManifest:
        return AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="Test",
            crons=[cron],
        )

    def test_unknown_timezone_is_a_validation_error(self) -> None:
        errors = self._manifest(
            CronEntry(name="daily", cron_expr="0 6 * * *", timezone="Mars/Olympus_Mons")
        ).validate()
        assert any("unknown timezone" in e and "daily" in e for e in errors)

    def test_malformed_skip_date_is_a_validation_error(self) -> None:
        errors = self._manifest(
            CronEntry(name="daily", cron_expr="0 6 * * *", skip_dates=["25/12/2026"])
        ).validate()
        assert any("invalid skip_date" in e and "daily" in e for e in errors)

    def test_non_padded_skip_date_is_rejected(self) -> None:
        """``2026-1-1`` parses but never matches the zero-padded fire-time form."""
        errors = self._manifest(
            CronEntry(name="daily", cron_expr="0 6 * * *", skip_dates=["2026-1-1"])
        ).validate()
        assert any("invalid skip_date" in e for e in errors)

    def test_valid_calendar_fields_produce_no_error(self) -> None:
        errors = self._manifest(
            CronEntry(
                name="market-open",
                cron_expr="30 9 * * 1-5",
                timezone="America/New_York",
                skip_dates=["2026-12-25"],
            )
        ).validate()
        assert not [e for e in errors if "timezone" in e or "skip_date" in e]

    def test_absent_calendar_fields_produce_no_error(self) -> None:
        errors = self._manifest(CronEntry(name="daily", cron_expr="0 6 * * *")).validate()
        assert not [e for e in errors if "timezone" in e or "skip_date" in e]
