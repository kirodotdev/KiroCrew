"""Uninstall `keep_specific` body parsing — malformed client input must not
leave an app partially uninstalled.

The dependency-cleanup step runs AFTER the onUninstall script has executed and
resources have been deregistered, so anything that raises there is not a clean
400 — it is a 500 with the app half-removed. `keep_specific` is unvalidated
client JSON, so it is sanitized at the parse boundary.
"""
from __future__ import annotations

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
