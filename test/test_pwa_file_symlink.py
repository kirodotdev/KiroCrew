"""Tests for the pwa_file handler's symlink-aware traversal guard."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import platform_compat


@pytest.mark.asyncio
async def test_pwa_file_serves_through_symlinked_dist(tmp_path):
    """dev-backend.sh symlinks static/dist -> KiroCrewWebsite/dist. The
    traversal guard must compare resolved paths on both sides so the
    legitimate symlinked file isn't rejected (falling through to the SPA
    fallback, which serves index.html as text/html and breaks any JS
    import e.g. /pcm-worklet.js)."""
    from kiro_crew.dashboard.handlers import core

    real_dist = tmp_path / "real-dist"
    real_dist.mkdir()
    (real_dist / "pcm-worklet.js").write_text("// worklet")

    link = tmp_path / "linked-dist"
    platform_compat.symlink_or_junction(real_dist, link)

    req = MagicMock()
    req.match_info = {"name": "pcm-worklet.js"}
    with patch.object(core, "_DIST_DIR", link):
        resp = await core.pwa_file(req)
    assert isinstance(resp, web.FileResponse)


@pytest.mark.asyncio
async def test_pwa_file_rejects_traversal(tmp_path):
    """Guard still blocks paths resolving outside _DIST_DIR."""
    from kiro_crew.dashboard.handlers import core

    dist = tmp_path / "dist"
    dist.mkdir()
    outside = tmp_path / "secret.js"
    outside.write_text("secret")
    (dist / "escape.js").symlink_to(outside)

    req = MagicMock()
    req.match_info = {"name": "escape.js"}
    with patch.object(core, "_DIST_DIR", dist):
        with pytest.raises(web.HTTPNotFound):
            await core.pwa_file(req)
