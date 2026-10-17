"""Property tests for AppContext permission mapping.

Feature: app-sdk-gateway-hooks
Properties 10, 11: Context permission mapping and base fields.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kiro_crew.apps.context import build_app_context

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def _app_name() -> st.SearchStrategy[str]:
    return st.from_regex(r"[a-z][a-z0-9-]{2,12}", fullmatch=True)


def _permissions() -> st.SearchStrategy[dict[str, Any]]:
    """Generate random permission configurations."""
    return st.fixed_dictionaries({
        "cron": st.booleans(),
        "storage": st.booleans(),
        "events": st.lists(
            st.from_regex(r"[a-z][a-z_]{2,10}", fullmatch=True),
            max_size=3,
        ),
    })


# ---------------------------------------------------------------------------
# Property 10: App context permission mapping
# ---------------------------------------------------------------------------


class TestAppContextPermissionMapping:
    """Property 10: App context permission mapping.

    **Validates: Requirements 5.1, 5.2, 5.3**
    """

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(app_name=_app_name(), perms=_permissions())
    def test_cron_present_iff_permitted(self, app_name: str, perms: dict[str, Any], tmp_path: Path) -> None:
        """ctx.cron is CronSDK iff permissions.cron is True."""
        mock_cron_svc = MagicMock()
        mock_cron_svc.list_jobs.return_value = []

        ctx = build_app_context(
            app_name=app_name,
            data_dir=tmp_path,
            permissions=perms,
            cron_service=mock_cron_svc,
            broadcast_fn=lambda x: None,
        )

        if perms["cron"]:
            assert ctx.cron is not None
            assert ctx.cron.app_name == app_name
        else:
            assert ctx.cron is None

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(app_name=_app_name(), perms=_permissions())
    def test_events_present_iff_permitted(self, app_name: str, perms: dict[str, Any], tmp_path: Path) -> None:
        """ctx.events is EventBus iff permissions.events is non-empty."""
        ctx = build_app_context(
            app_name=app_name,
            data_dir=tmp_path,
            permissions=perms,
            cron_service=MagicMock(),
            broadcast_fn=lambda x: None,
        )

        if perms["events"]:
            assert ctx.events is not None
            assert ctx.events.app_name == app_name
        else:
            assert ctx.events is None

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(app_name=_app_name(), perms=_permissions())
    def test_storage_present_iff_permitted(self, app_name: str, perms: dict[str, Any], tmp_path: Path) -> None:
        """ctx.storage is AppStorage iff permissions.storage is True."""
        ctx = build_app_context(
            app_name=app_name,
            data_dir=tmp_path,
            permissions=perms,
            cron_service=MagicMock(),
            broadcast_fn=lambda x: None,
        )

        if perms["storage"]:
            assert ctx.storage is not None
            assert ctx.storage.app_name == app_name
        else:
            assert ctx.storage is None


# ---------------------------------------------------------------------------
# Property 11: App context base fields always present
# ---------------------------------------------------------------------------


class TestAppContextBaseFields:
    """Property 11: App context base fields always present.

    **Validates: Requirements 5.4, 5.5**
    """

    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(app_name=_app_name(), perms=_permissions())
    def test_base_fields_always_present(self, app_name: str, perms: dict[str, Any], tmp_path: Path) -> None:
        """name, data_dir, and logger are always populated regardless of permissions."""
        ctx = build_app_context(
            app_name=app_name,
            data_dir=tmp_path,
            permissions=perms,
            cron_service=MagicMock(),
            broadcast_fn=lambda x: None,
        )

        assert ctx.name == app_name
        assert ctx.name  # non-empty
        assert ctx.data_dir == tmp_path
        assert ctx.logger is not None
        assert app_name in ctx.logger.name
