"""Uninstall must drop the app's in-process hook registrations.

``apps/teardown.py`` keeps three per-app registries in process memory —
``_APP_DISABLE_HOOKS``, ``_SLOT_CLOSE_HOOKS``, ``_SLOT_CLOSE_UNDO_HOOKS`` — and
ships an ``unregister_*`` primitive for each. Nothing called any of them, so an
uninstall left the removed app's closures installed over a store the same handler
had just deleted.

That is load-bearing rather than untidy. ``notify_slot_closed`` reports a hook
failure instead of swallowing it, and ``api_chat_slot_delete`` REFUSES the
dismissal on a false return — deliberately, so a dismissed tab can never outlive
a still-running worker. Applied to a stale hook the same rule inverts: a slot
belonging to an uninstalled app can no longer be closed at all, because the hook
that must approve the close belongs to an app that is gone.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.apps.teardown as teardown

APP = "test-app"


@pytest.fixture(autouse=True)
def _clean_registries():
    """The registries are module globals; never leak one into another test."""
    teardown.forget_app_hooks(APP)
    yield
    teardown.forget_app_hooks(APP)


def _install_hooks(record: list[str]):
    """Register one hook of each kind for APP, all recording that they ran."""

    async def _disable(app: str) -> None:
        record.append("disable")

    async def _closed(slot_key: str) -> None:
        record.append("closed")

    async def _undone(slot_key: str) -> None:
        record.append("undone")

    teardown.register_app_disable_hook(APP, _disable)
    teardown.register_slot_close_hook(APP, _closed)
    teardown.register_slot_close_undo_hook(APP, _undone)


def _registered() -> dict[str, bool]:
    return {
        "disable": APP in teardown._APP_DISABLE_HOOKS,
        "close": APP in teardown._SLOT_CLOSE_HOOKS,
        "undo": APP in teardown._SLOT_CLOSE_UNDO_HOOKS,
    }


class TestForgetAppHooks:
    """The primitive itself, before anything wires it into a handler."""

    def test_it_drops_all_three_registries(self):
        _install_hooks([])
        assert all(_registered().values()), "fixture must actually register"

        teardown.forget_app_hooks(APP)

        assert _registered() == {"disable": False, "close": False, "undo": False}

    def test_it_leaves_other_apps_alone(self):
        """Keyed by app name — uninstalling one app must not disarm another."""
        other_ran: list[str] = []

        async def _other(slot_key: str) -> None:
            other_ran.append(slot_key)

        _install_hooks([])
        teardown.register_slot_close_hook("other-app", _other)
        try:
            teardown.forget_app_hooks(APP)
            assert "other-app" in teardown._SLOT_CLOSE_HOOKS
        finally:
            teardown.unregister_slot_close_hook("other-app")

    def test_it_is_safe_when_nothing_is_registered(self):
        """Uninstall runs for apps that never registered anything."""
        teardown.forget_app_hooks("never-registered-app")


@pytest.mark.asyncio
class TestUninstallDropsTheHooks:
    async def _uninstall(self) -> None:
        """Drive handle_uninstall_app for APP with the surrounding I/O stubbed."""
        fake_app = {
            "name": APP,
            "manifest": {},
            "resources": "gateway",
            "lifecycle": "normal",
            "enabled": False,
        }
        request = MagicMock()
        request.match_info = {"name": APP}
        request.app = {"state": MagicMock()}
        request.json = AsyncMock(return_value={})

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app),
            patch(
                "kiro_crew.apps.routes.uninstall_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
            ),
            patch("kiro_crew.apps.routes.stop_app_backend", return_value=None),
            patch("kiro_crew.apps.routes.deregister_app", return_value=None),
            patch(
                "kiro_crew.apps.teardown.on_app_disable",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch(
                "kiro_crew.apps.routes.classify_and_clean_for_uninstall",
                return_value={"removable": [], "shared": [], "userInstalled": []},
            ),
            patch(
                "kiro_crew.apps.routes.clean_dependencies",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            from kiro_crew.apps.routes import handle_uninstall_app

            resp = await handle_uninstall_app(request)
        assert resp.status < 400, f"uninstall failed with {resp.status}"

    async def test_uninstall_leaves_no_hook_behind(self):
        """Red before the fix: all three survive the uninstall."""
        _install_hooks([])
        await self._uninstall()
        assert _registered() == {"disable": False, "close": False, "undo": False}

    async def test_a_leftover_slot_can_still_be_dismissed_after_uninstall(self):
        """The user-visible consequence, stated as the property that matters.

        A hook whose app was uninstalled raises — its store is gone — and
        ``notify_slot_closed`` turns that into ``False``, which
        ``api_chat_slot_delete`` treats as "refuse the close". Asserting the
        registry is empty would pass on a fix that merely swapped one registry;
        asserting the ANSWER is what pins the behaviour the user feels.
        """

        async def _hook_over_deleted_store(slot_key: str) -> None:
            raise FileNotFoundError("app store was removed by uninstall")

        teardown.register_slot_close_hook(APP, _hook_over_deleted_store)

        # Before the uninstall the stale hook is what refuses the dismissal.
        assert await teardown.notify_slot_closed(APP, "slot-1") is False

        await self._uninstall()

        # After it, no hook is registered, so the close is allowed to proceed.
        assert await teardown.notify_slot_closed(APP, "slot-1") is True

    async def test_the_disable_hook_is_not_dropped_by_a_plain_disable(self):
        """Scope control: only uninstall is terminal.

        A disabled app can be switched back on and the registries are repopulated
        by each app's own watchdog, so clearing them on disable would depend on
        that watchdog having run again before the next dismissal. This asserts the
        narrower scope rather than leaving it to the description.
        """
        _install_hooks([])
        await teardown.notify_app_disabled(APP)
        assert _registered() == {"disable": True, "close": True, "undo": True}
