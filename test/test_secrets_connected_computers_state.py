"""The Secrets routes neither list nor edit connected computers' own state.

``CONNECTED_COMPUTERS_LOCAL`` (this computer's grants, install identity and
queued remote revokes) and ``CONNECTED_COMPUTERS_DEVICES`` (a gateway's enrolled
computers) are compare-and-swap JSON documents in the vault. A generic replace
or delete would destroy that state, and a value that is not the document makes
Off fail before it stops anything, so both names are reserved the way the other
managed entries are (``_managed_name_owned``), for the routes and for the
free-name check other writers use (``reserved_name_check``).

Not run locally (host rule); CI runs it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request
from test_secrets_handler import _app

from kiro_crew.connected_computers.devices import DEVICES_ENTRY
from kiro_crew.connected_computers.local_store import LOCAL_ENTRY
from kiro_crew.secrets import SecretVault

pytestmark = pytest.mark.asyncio

STATE = '{"version":1}'
_CONFIG_DIR = "kiro_crew.dashboard.handlers.secrets.config_dir"


@pytest.fixture()
def vault_dir(tmp_path: Path) -> Path:
    vault = SecretVault(tmp_path)
    vault._set_sync(LOCAL_ENTRY, STATE)
    vault._set_sync(DEVICES_ENTRY, STATE)
    vault._set_sync("MY_TOKEN", "value")
    return tmp_path


@pytest.mark.parametrize("name", [LOCAL_ENTRY, DEVICES_ENTRY])
@pytest.mark.parametrize("method", ["post", "delete"])
async def test_a_generic_write_or_delete_of_the_state_is_refused(vault_dir, name, method):
    with patch(_CONFIG_DIR, return_value=str(vault_dir)):
        async with TestClient(TestServer(_app())) as client:
            if method == "post":
                resp = await client.post("/api/secrets", json={"name": name, "value": "x"})
            else:
                resp = await client.delete(f"/api/secrets/{name}")
            assert resp.status == 409, await resp.text()
            assert (await resp.json())["code"] == "managed_by_connected_computers"
    stored = SecretVault(vault_dir).get(name)
    assert stored is not None and stored.reveal() == STATE


@pytest.mark.parametrize("name", [LOCAL_ENTRY, DEVICES_ENTRY])
async def test_a_typed_item_cannot_take_the_state_name_either(vault_dir, name):
    with patch(_CONFIG_DIR, return_value=str(vault_dir)):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/secrets",
                json={"name": f"  {name} ", "type": "note", "fields": {"text": "x"}},
            )
            assert resp.status == 409
    assert SecretVault(vault_dir).get(name).reveal() == STATE


async def test_the_state_is_left_out_of_the_listing(vault_dir):
    with patch(_CONFIG_DIR, return_value=str(vault_dir)):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.get("/api/secrets")
            assert resp.status == 200
            body = await resp.json()
    assert body["names"] == ["MY_TOKEN"]
    assert [item["name"] for item in body["items"]] == ["MY_TOKEN"]
    assert all(entry["name"] not in (LOCAL_ENTRY, DEVICES_ENTRY) for entry in body["managed"])


async def test_the_free_name_check_reserves_the_state(tmp_path):
    from kiro_crew.dashboard.handlers.secrets import reserved_name_check

    request = make_mocked_request("POST", "/api/browser/ask/answer")
    with patch(_CONFIG_DIR, return_value=str(tmp_path)):
        reserved = await reserved_name_check(request)
    assert reserved(LOCAL_ENTRY) and reserved(DEVICES_ENTRY)
    assert not reserved("MY_TOKEN")
