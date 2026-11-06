"""PATCH /api/config/kirocrew and ``dashboard.remote_editor.*``.

Settings → Display → Remote editor. Both values become part of the file-path
menus' ``<editor>://vscode-remote/ssh-remote+<host><path>`` link, so the write
gate takes only a listed editor and a host that is a DNS name or SSH alias.
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_config_patch import _make_app, _patch, tmp_config  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,value,status",
    [
        ("dashboard.remote_editor.editor", "vscode", 200),
        ("dashboard.remote_editor.editor", "", 200),
        # Becomes the link's URL scheme, so only a listed editor is written.
        ("dashboard.remote_editor.editor", "javascript", 400),
        ("dashboard.remote_editor.host", "dev-box.example.com", 200),
        # A port, path or query would add URL structure after ssh-remote+.
        ("dashboard.remote_editor.host", "dev-box:22", 400),
        ("dashboard.remote_editor.host", "dev-box/x?y", 400),
        # The editor's ssh could read a leading dash as an option.
        ("dashboard.remote_editor.host", "-Jjump.example.com", 400),
    ],
)
async def test_write_gate(tmp_config, path, value, status) -> None:  # noqa: F811
    async with TestClient(TestServer(_make_app())) as client:
        resp = await _patch(client, path, value)
        assert resp.status == status
    if status == 200:
        data = json.loads(tmp_config.read_text(encoding="utf-8"))
        assert data["dashboard"]["remote_editor"][path.rsplit(".", 1)[1]] == value


@pytest.mark.parametrize(
    "raw,loaded",
    [
        # A saved choice survives load, so branding can serve it.
        ({"editor": "vscode", "host": "dev-box"}, {"editor": "vscode", "host": "dev-box"}),
        # A hand-edited scalar falls back to the off default instead of crashing load.
        ("corrupt", {"editor": "", "host": ""}),
    ],
)
def test_load_reads_the_saved_remote_editor(raw, loaded) -> None:
    from config_loader_helpers import _load_from_dict

    cfg = _load_from_dict({"dashboard": {"remote_editor": raw}})
    assert cfg.dashboard.remote_editor == loaded
