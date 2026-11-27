"""An app token reaches only the app it names, whatever the request path spells.

The ``/api/apps/{name}/...`` routes take the app name from the URL. aiohttp decodes
an escaped ``/`` inside that segment (``a%2F..%2Fb``), so the decoded name can hold
``/`` and ``..``: joined onto the apps folder it would point at another app's tree,
while the decoded request path still starts with the caller's own ``/api/apps/a/``.

These run a real app token through the real ``token_auth_middleware`` and the
production route table, with two installed apps on disk, and pin that:

* an app's own token still reads and writes its own config;
* a name that is not one plain path segment is refused, and the other app's
  ``config.json`` is neither read nor changed;
* an app path the other app's manifest grant names still works;
* ``app_dir`` refuses such a name, so no caller can join it onto the apps folder,
  and still takes an older installed name and the gateway's ``.`` sidecar names.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.apps import host_exec_grants
from kiro_crew.apps.manager_records import InstalledApp, _write_installed, app_dir
from kiro_crew.dashboard import revocation_gen, token_auth

OWN = "app-one"
OTHER = "app-two"
_OTHER_CONFIG = {"kept": True}


@pytest.fixture
def crew(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "crew"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"app-name-segment-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    for name in (OWN, OTHER):
        root = app_dir(name)
        (root / "data").mkdir(parents=True)
        (root / "app.json").write_text(
            json.dumps({"name": name, "version": "1.0.0", "displayName": name}),
            encoding="utf-8",
        )
        _write_installed(
            name,
            InstalledApp(name=name, version="1.0.0", enabled=True, origin="external"),
        )
        assert host_exec_grants.grant_host_exec(name)
    (app_dir(OTHER) / "data" / "config.json").write_text(
        json.dumps(_OTHER_CONFIG) + "\n", encoding="utf-8"
    )
    return home


def _server() -> web.Application:
    from kiro_crew.dashboard.routes import system

    app = web.Application(middlewares=[token_auth.token_auth_middleware()])
    app["state"] = SimpleNamespace(owner_id="owner", broadcast_ws=lambda *a, **k: None)
    system.register(app)
    return app


async def _call(method: str, raw_path: str, body: Any = None) -> tuple[int, Any]:
    from yarl import URL

    token = token_auth.generate_token(OWN, app=OWN)
    async with TestClient(TestServer(_server())) as client:
        # Sent as written: the escapes in *raw_path* reach the server unchanged.
        url = client.make_url("").join(URL(raw_path, encoded=True)).with_query(token=token)
        resp = await client.session.request(method, url, json=body)
        text = await resp.text()
        try:
            return resp.status, json.loads(text)
        except ValueError:
            return resp.status, text


@pytest.mark.asyncio
async def test_an_apps_own_token_reads_and_writes_its_own_config(crew: Path) -> None:
    status, payload = await _call("PUT", f"/api/apps/{OWN}/config", {"mine": 1})
    assert status == 200, payload
    status, payload = await _call("GET", f"/api/apps/{OWN}/config")
    assert (status, payload) == (200, {"mine": 1})


@pytest.mark.parametrize(
    "segment",
    [
        f"{OWN}%2F..%2F{OTHER}",
        f"{OWN}%2f..%2f{OTHER}",
        f"{OWN}%2F%2E%2E%2F{OTHER}",
        f"{OWN}%5C..%5C{OTHER}",
        f"{OWN}%252F..%252F{OTHER}",
        f"{OWN}%00",
        f"{OWN}/%2E%2E/{OTHER}",
        f"{OWN}/%2e%2e/{OTHER}",
        f"{OWN}/../{OTHER}",
    ],
)
@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.asyncio
async def test_a_name_that_is_not_one_segment_reaches_no_other_app(
    crew: Path, segment: str, method: str
) -> None:
    other = app_dir(OTHER) / "data" / "config.json"
    before = other.read_bytes()
    status, payload = await _call(method, f"/api/apps/{segment}/config", {"planted": 1})
    assert status in (400, 403, 404), (status, payload)
    assert payload != _OTHER_CONFIG
    assert other.read_bytes() == before


@pytest.mark.parametrize("name", [".", "..", "a/b", "../x", "a\\b", "a\x00b"])
def test_app_dir_refuses_a_name_that_is_not_one_segment(crew: Path, name: str) -> None:
    with pytest.raises(ValueError):
        app_dir(name)


@pytest.mark.asyncio
async def test_a_granted_path_of_another_app_still_works(
    crew: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(token_auth, "_app_api_allowlist", lambda name: (f"/api/apps/{OTHER}/*",))
    status, payload = await _call("GET", f"/api/apps/{OTHER}/config")
    assert (status, payload) == (200, _OTHER_CONFIG)


@pytest.mark.parametrize(
    "name", ["Legacy_App", "a.b", ".app-one-update-inflight", ".app-one-data-tmp"]
)
def test_app_dir_takes_older_names_and_sidecar_names(crew: Path, name: str) -> None:
    assert app_dir(name).parent == app_dir(OWN).parent
