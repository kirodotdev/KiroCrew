"""Property tests for manifest hooks and extended CronEntry.

Feature: app-sdk-gateway-hooks
Properties 12, 13: Hook path validation and manifest round-trip.
"""
from __future__ import annotations

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
