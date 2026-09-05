"""Uninstall `keep_specific` body parsing — malformed client input must not
leave an app partially uninstalled.

The dependency-cleanup step runs AFTER the onUninstall script has executed and
resources have been deregistered, so anything that raises there is not a clean
400 — it is a 500 with the app half-removed. `keep_specific` is unvalidated
client JSON, so it is sanitized at the parse boundary.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.apps.dependency_ledger import canonical_dep_key


class TestCanonicalDepKeyRobustness:
    @pytest.mark.parametrize("bad", [None, 5, 0, {"a": 1}, ["x"], True])
    def test_non_string_input_does_not_raise(self, bad):
        """Defense in depth for the boundary filter: a non-string must degrade,
        not raise, so no future caller can turn bad input into a 500."""
        assert canonical_dep_key(bad) == bad  # type: ignore[arg-type]


@pytest.mark.asyncio
class TestUninstallKeepSpecificParsing:
    async def _run(self, body: object) -> MagicMock:
        """Drive handle_uninstall_app with *body* and return the deps mock."""
        fake_app = {
            "name": "test-app",
            "manifest": {"dependencies": {"capabilities": {"mcp": ["dep-a"]}}},
            "resources": "gateway",
            "lifecycle": "normal",
            "enabled": False,
        }
        request = MagicMock()
        request.match_info = {"name": "test-app"}
        request.app = {"state": MagicMock()}
        request.json = AsyncMock(return_value=body)

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app),
            patch("kiro_crew.apps.routes.uninstall_app", return_value=MagicMock(
                ok=True, to_dict=lambda: {"ok": True})),
            patch("kiro_crew.apps.routes.stop_app_backend", return_value=None),
            patch("kiro_crew.apps.routes.deregister_app", return_value=None),
            patch("kiro_crew.apps.teardown.on_app_disable", new_callable=AsyncMock,
                  return_value=None),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch("kiro_crew.apps.routes.classify_and_clean_for_uninstall",
                  return_value={"removable": [], "shared": [], "userInstalled": []}) as m,
            patch("kiro_crew.apps.routes.clean_dependencies", new_callable=AsyncMock,
                  return_value=[]),
        ):
            from kiro_crew.apps.routes import handle_uninstall_app

            resp = await handle_uninstall_app(request)
        assert resp.status < 500, f"malformed body produced {resp.status}"
        return m

    async def test_null_entry_does_not_500(self):
        """The reported crash: [null] reached canonical_dep_key and raised."""
        m = await self._run({"keep_specific": [None]})
        assert m.call_args.kwargs["keep_specific"] == []

    async def test_mixed_junk_is_filtered_to_strings(self):
        m = await self._run({"keep_specific": ["capability/mcp/a", None, 5, "", {"x": 1}]})
        assert m.call_args.kwargs["keep_specific"] == ["capability/mcp/a"]

    async def test_non_list_is_ignored(self):
        m = await self._run({"keep_specific": "capability/mcp/a"})
        assert m.call_args.kwargs["keep_specific"] == []

    async def test_legacy_key_is_still_normalized(self):
        """Sanitizing must not break the legacy-id normalization it wraps."""
        m = await self._run({"keep_specific": ["aim/mcp/a"]})
        assert m.call_args.kwargs["keep_specific"] == ["capability/mcp/a"]


@pytest.mark.asyncio
class TestUninstallLedgerPreFlight:
    async def test_unreadable_ledger_refuses_before_teardown(self, tmp_path, monkeypatch):
        """A corrupt ledger must cost a handled refusal BEFORE the teardown.

        classify_and_clean_for_uninstall reads the ledger strictly, and that
        read fires after the onUninstall script and deregistration — a
        mid-flow failure there strands a half-uninstalled app and a retry
        reruns teardown. The pre-flight mirrors the trust-grant precondition:
        refuse free, keep the retry safe.
        """
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew-home"))
        from kiro_crew.apps.dependency_ledger import _ledger_path

        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"capability/mcp/dep-a": {"installedBy": ["test-app"], "in')

        fake_app = {
            "name": "test-app",
            "manifest": {"dependencies": {"capabilities": {"mcp": ["dep-a"]}}},
            "resources": "gateway",
            "lifecycle": "normal",
            "enabled": False,
        }
        request = MagicMock()
        request.match_info = {"name": "test-app"}
        request.app = {"state": MagicMock()}
        request.json = AsyncMock(return_value={})

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app),
            patch(
                "kiro_crew.apps.routes.uninstall_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
            ) as m_uninstall,
            patch("kiro_crew.apps.routes.stop_app_backend", return_value=None),
            patch("kiro_crew.apps.routes.deregister_app", return_value=None),
            patch(
                "kiro_crew.apps.teardown.on_app_disable", new_callable=AsyncMock, return_value=None
            ),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch(
                "kiro_crew.apps.routes.classify_and_clean_for_uninstall",
                return_value={"removable": [], "shared": [], "userInstalled": []},
            ) as m_classify,
            patch(
                "kiro_crew.apps.routes.clean_dependencies", new_callable=AsyncMock, return_value=[]
            ),
        ):
            from kiro_crew.apps.routes import handle_uninstall_app

            resp = await handle_uninstall_app(request)

        assert resp.status == 500
        payload = json.loads(resp.text)
        assert payload["code"] == "dependency_ledger_unreadable"
        # Nothing destructive ran: the refusal is free and the retry is safe.
        m_uninstall.assert_not_called()
        m_classify.assert_not_called()

    async def test_keep_dependencies_skips_the_pre_flight(self, tmp_path, monkeypatch):
        """A purge-only uninstall never touches the ledger, so an unreadable
        one must not block it."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "kirocrew-home"))
        from kiro_crew.apps.dependency_ledger import _ledger_path

        path = _ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"capability/mcp/dep-a": {"installedBy": ["test-app"], "in')

        fake_app = {
            "name": "test-app",
            "manifest": {"dependencies": {"capabilities": {"mcp": ["dep-a"]}}},
            "resources": "gateway",
            "lifecycle": "normal",
            "enabled": False,
        }
        request = MagicMock()
        request.match_info = {"name": "test-app"}
        request.app = {"state": MagicMock()}
        request.json = AsyncMock(return_value={"keep_dependencies": True})

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app),
            patch(
                "kiro_crew.apps.routes.uninstall_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
            ),
            patch("kiro_crew.apps.routes.stop_app_backend", return_value=None),
            patch("kiro_crew.apps.routes.deregister_app", return_value=None),
            patch(
                "kiro_crew.apps.teardown.on_app_disable", new_callable=AsyncMock, return_value=None
            ),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch(
                "kiro_crew.apps.routes.classify_and_clean_for_uninstall",
                return_value={"removable": [], "shared": [], "userInstalled": []},
            ) as m_classify,
            patch(
                "kiro_crew.apps.routes.clean_dependencies", new_callable=AsyncMock, return_value=[]
            ),
        ):
            from kiro_crew.apps.routes import handle_uninstall_app

            resp = await handle_uninstall_app(request)

        assert resp.status < 500
        m_classify.assert_not_called()

    async def test_ledger_corrupted_mid_teardown_finishes_the_uninstall(self):
        """The pre-flight read a healthy ledger, but the onUninstall script is
        arbitrary code and can truncate it mid-teardown. The uninstall must
        then FINISH without ledger cleanup -- failing here strands a
        half-uninstalled app whose retry refuses at the pre-flight forever."""
        fake_app = {
            "name": "test-app",
            "manifest": {"dependencies": {"capabilities": {"mcp": ["dep-a"]}}},
            "resources": "gateway",
            "lifecycle": "normal",
            "enabled": False,
        }
        request = MagicMock()
        request.match_info = {"name": "test-app"}
        request.app = {"state": MagicMock()}
        request.json = AsyncMock(return_value={})

        with (
            patch("kiro_crew.apps.routes.get_app", return_value=fake_app),
            patch(
                "kiro_crew.apps.routes.uninstall_app",
                return_value=MagicMock(ok=True, to_dict=lambda: {"ok": True}),
            ) as m_uninstall,
            patch("kiro_crew.apps.routes.stop_app_backend", return_value=None),
            patch("kiro_crew.apps.routes.deregister_app", return_value=None),
            patch(
                "kiro_crew.apps.teardown.on_app_disable", new_callable=AsyncMock, return_value=None
            ),
            patch("kiro_crew.apps.routes.sel", return_value=MagicMock()),
            patch(
                "kiro_crew.apps.routes.classify_and_clean_for_uninstall",
                side_effect=PermissionError(13, "Permission denied"),
            ) as m_classify,
            patch(
                "kiro_crew.apps.routes.clean_dependencies", new_callable=AsyncMock, return_value=[]
            ) as m_clean,
        ):
            from kiro_crew.apps.routes import handle_uninstall_app

            resp = await handle_uninstall_app(request)

        # The uninstall completes; the ledger is never published over.
        assert resp.status < 500
        m_classify.assert_called_once()
        m_clean.assert_not_called()
        m_uninstall.assert_called_once()
