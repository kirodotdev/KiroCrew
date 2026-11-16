"""An optional rotate-by date on a vault entry, set through the Secrets route.

The date sits in the clear beside the entry's ciphertext, like ``updated_at``:
it is never stored with, derived from or logged next to the value. An entry
written without one reads exactly as before, and a malformed date in a request
is dropped (and logged) rather than refused.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers.secrets import setup_secrets_routes
from kiro_crew.secrets import SecretVault
from kiro_crew.secrets.vault import entry_revision

_CONFIG_DIR = "kiro_crew.dashboard.handlers.secrets.config_dir"
_PREVIEW = "kiro_crew.dashboard.handlers.secrets._secrets_vault_preview_on"


class _OwnerState:
    owner_id = ""


def _app() -> web.Application:
    """The secrets routes behind the implicit local owner (no owner configured)."""

    @web.middleware
    async def _identity(request, handler):
        request["user"] = "local-app"
        request["app"] = ""
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    app["state"] = _OwnerState()
    setup_secrets_routes(app)
    return app


def _entries(tmp_path: Path) -> dict:
    return json.loads((tmp_path / ".vault" / "secrets.enc").read_text())["entries"]


# ── vault seam ──


def test_an_entry_written_with_a_date_round_trips_beside_its_ciphertext(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    revision = asyncio.run(
        vault.set_item_with_revision(
            "API", "secret", {"value": "synthetic-1"}, rotate_by="2026-12-01"
        )
    )
    entry = _entries(tmp_path)["API"]
    assert set(entry) == {"nonce", "ct", "rotate_by"}
    assert entry["rotate_by"] == "2026-12-01"
    assert revision == entry_revision(entry)
    assert vault.get("API").reveal() == "synthetic-1"  # type: ignore[union-attr]
    (row,) = vault.list_items()
    assert "rotate_by" not in row and row["revision"] == revision
    assert vault.rotate_by_dates() == {"API": "2026-12-01"}


def test_a_typed_item_carries_its_date_too(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    asyncio.run(vault.set_item_with_revision("N", "note", {"text": "t"}, rotate_by="2027-01-31"))
    assert vault.rotate_by_dates() == {"N": "2027-01-31"}
    assert vault.get_items_many(["N"])["N"] is not None


def test_a_write_without_a_date_leaves_main_shaped_entries(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    asyncio.run(vault.set_item_with_revision("API", "secret", {"value": "synthetic-1"}))
    vault.set_sync("PLAIN", "synthetic-2")
    entries = _entries(tmp_path)
    assert set(entries["API"]) == set(entries["PLAIN"]) == {"nonce", "ct"}
    assert all("rotate_by" not in row for row in vault.list_items())
    assert vault.rotate_by_dates() == {}


def test_saving_a_new_value_without_a_date_clears_the_old_one(tmp_path: Path) -> None:
    vault = SecretVault(tmp_path)
    asyncio.run(
        vault.set_item_with_revision("API", "secret", {"value": "old"}, rotate_by="2020-01-01")
    )
    asyncio.run(vault.set_item_with_revision("API", "secret", {"value": "new"}))
    assert vault.rotate_by_dates() == {}
    assert "rotate_by" not in _entries(tmp_path)["API"]


@pytest.mark.parametrize("stored", [123, None, "soon", "2026-13-01", "2026-02-30", "20261201"])
def test_a_hand_edited_date_that_is_not_one_reads_as_none(tmp_path: Path, stored: object) -> None:
    vault = SecretVault(tmp_path)
    vault.set_sync("API", "synthetic-1")
    store = tmp_path / ".vault" / "secrets.enc"
    envelope = json.loads(store.read_text())
    envelope["entries"]["API"]["rotate_by"] = stored
    store.write_text(json.dumps(envelope))
    assert vault.rotate_by_dates() == {}
    (row,) = vault.list_items()
    assert "rotate_by" not in row
    assert vault.get("API").reveal() == "synthetic-1"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-12-01", "2026-12-01"),
        ("2024-02-29", "2024-02-29"),
        ("2026-02-29", None),
        ("2026-12-1", None),
        (" 2026-12-01", None),
        ("2026-12-01 ", None),
        ("2026/12/01", None),
        ("20261201", None),
        ("\uff12\uff10\uff12\uff16-12-01", None),
        ("", None),
        (20261201, None),
        (None, None),
    ],
)
def test_parse_rotate_by_accepts_only_a_calendar_date(value: object, expected: object) -> None:
    from kiro_crew.secrets.vault import parse_rotate_by

    assert parse_rotate_by(value) == expected


# ── route seam ──


@pytest.mark.asyncio
async def test_a_main_shaped_entry_lists_exactly_as_before(tmp_path: Path) -> None:
    """Passes on main: no request field and no stored date leave every answer unchanged."""
    with patch(_CONFIG_DIR, return_value=str(tmp_path)):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post("/api/secrets", json={"name": "K", "value": "synthetic-1"})
            assert resp.status == 200
            body = await resp.json()
            assert set(body) == {"ok", "name", "type", "revision"}
            assert set(_entries(tmp_path)["K"]) == {"nonce", "ct"}
            listing = await (await client.get("/api/secrets")).json()
            assert listing == {"names": ["K"], "managed": []}


@pytest.mark.asyncio
async def test_the_route_sets_and_lists_a_date(tmp_path: Path) -> None:
    with patch(_CONFIG_DIR, return_value=str(tmp_path)):
        async with TestClient(TestServer(_app())) as client:
            resp = await client.post(
                "/api/secrets",
                json={"name": "K", "value": "synthetic-1", "rotate_by": "2026-12-01"},
            )
            assert resp.status == 200
            assert "warning" not in await resp.json()
            listing = await (await client.get("/api/secrets")).json()
            assert listing["rotate_by"] == {"K": "2026-12-01"}
            with patch(_PREVIEW, return_value=True):
                listing = await (await client.get("/api/secrets")).json()
            assert listing["rotate_by"] == {"K": "2026-12-01"}
            (item,) = listing["items"]
            assert "rotate_by" not in item


@pytest.mark.asyncio
@pytest.mark.parametrize("cleared", [None, ""])
async def test_the_route_clears_a_date(tmp_path: Path, cleared: object) -> None:
    with patch(_CONFIG_DIR, return_value=str(tmp_path)):
        async with TestClient(TestServer(_app())) as client:
            await client.post(
                "/api/secrets",
                json={"name": "K", "value": "synthetic-1", "rotate_by": "2026-12-01"},
            )
            resp = await client.post(
                "/api/secrets", json={"name": "K", "value": "synthetic-2", "rotate_by": cleared}
            )
            assert resp.status == 200
            assert "warning" not in await resp.json()
            listing = await (await client.get("/api/secrets")).json()
            assert "rotate_by" not in listing
            assert SecretVault(tmp_path).get("K").reveal() == "synthetic-2"  # type: ignore[union-attr]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["next week", "2026-02-30", 20261201, ["2026-12-01"]])
async def test_a_malformed_date_is_dropped_and_logged(
    tmp_path: Path, bad: object, caplog: pytest.LogCaptureFixture
) -> None:
    with patch(_CONFIG_DIR, return_value=str(tmp_path)):
        async with TestClient(TestServer(_app())) as client:
            with caplog.at_level("WARNING", logger="kiro_crew.dashboard.handlers.secrets"):
                resp = await client.post(
                    "/api/secrets", json={"name": "K", "value": "synthetic-1", "rotate_by": bad}
                )
            assert resp.status == 200
            body = await resp.json()
            assert set(body) == {"ok", "name", "type", "revision"}
            (record,) = [r for r in caplog.records if "rotate_by was dropped" in r.getMessage()]
            assert record.levelname == "WARNING" and "YYYY-MM-DD" in record.getMessage()
            assert set(_entries(tmp_path)["K"]) == {"nonce", "ct"}
            assert SecretVault(tmp_path).get("K").reveal() == "synthetic-1"  # type: ignore[union-attr]
